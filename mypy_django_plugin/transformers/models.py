from typing import Dict, List, Optional, Type, Union, cast

from django.db.models.base import Model
from django.db.models.fields import DateField, DateTimeField
from django.db.models.fields.related import ForeignKey
from django.db.models.fields.reverse_related import ManyToManyRel, ManyToOneRel, OneToOneRel
from mypy.checker import TypeChecker
from mypy.nodes import ARG_STAR2, Argument, Context, FuncDef, TypeInfo, Var
from mypy.plugin import AnalyzeTypeContext, AttributeContext, CheckerPluginInterface, ClassDefContext
from mypy.plugins import common
from mypy.semanal import SemanticAnalyzer
from mypy.types import AnyType, Instance
from mypy.types import Type as MypyType
from mypy.types import TypedDictType, TypeOfAny

from mypy_django_plugin.django.context import DjangoContext
from mypy_django_plugin.lib import fullnames, helpers
from mypy_django_plugin.lib.fullnames import ANNOTATIONS_FULLNAME, ANY_ATTR_ALLOWED_CLASS_FULLNAME
from mypy_django_plugin.lib.helpers import add_new_class_for_module
from mypy_django_plugin.transformers import fields
from mypy_django_plugin.transformers.fields import get_field_descriptor_types


class ModelClassInitializer:
    api: SemanticAnalyzer

    def __init__(self, ctx: ClassDefContext, django_context: DjangoContext):
        self.api = cast(SemanticAnalyzer, ctx.api)
        self.model_classdef = ctx.cls
        self.django_context = django_context
        self.ctx = ctx

    def lookup_typeinfo(self, fullname: str) -> Optional[TypeInfo]:
        return helpers.lookup_fully_qualified_typeinfo(self.api, fullname)

    def lookup_typeinfo_or_incomplete_defn_error(self, fullname: str) -> TypeInfo:
        info = self.lookup_typeinfo(fullname)
        if info is None:
            raise helpers.IncompleteDefnException(f"No {fullname!r} found")
        return info

    def lookup_class_typeinfo_or_incomplete_defn_error(self, klass: type) -> TypeInfo:
        fullname = helpers.get_class_fullname(klass)
        field_info = self.lookup_typeinfo_or_incomplete_defn_error(fullname)
        return field_info

    def create_new_var(self, name: str, typ: MypyType) -> Var:
        # type=: type of the variable itself
        var = Var(name=name, type=typ)
        # var.info: type of the object variable is bound to
        var.info = self.model_classdef.info
        var._fullname = self.model_classdef.info.fullname + "." + name
        var.is_initialized_in_class = True
        var.is_inferred = True
        return var

    def add_new_node_to_model_class(self, name: str, typ: MypyType) -> None:
        helpers.add_new_sym_for_info(self.model_classdef.info, name=name, sym_type=typ)

    def add_new_class_for_current_module(self, name: str, bases: List[Instance]) -> TypeInfo:
        current_module = self.api.modules[self.model_classdef.info.module_name]
        new_class_info = helpers.add_new_class_for_module(current_module, name=name, bases=bases)
        return new_class_info

    def run(self) -> None:
        model_cls = self.django_context.get_model_class_by_fullname(self.model_classdef.fullname)
        if model_cls is None:
            return
        self.run_with_model_cls(model_cls)

    def run_with_model_cls(self, model_cls):
        pass


class InjectAnyAsBaseForNestedMeta(ModelClassInitializer):
    """
    Replaces
        class MyModel(models.Model):
            class Meta:
                pass
    with
        class MyModel(models.Model):
            class Meta(Any):
                pass
    to get around incompatible Meta inner classes for different models.
    """

    def run(self) -> None:
        meta_node = helpers.get_nested_meta_node_for_current_class(self.model_classdef.info)
        if meta_node is None:
            return None
        meta_node.fallback_to_any = True


class AddDefaultPrimaryKey(ModelClassInitializer):
    def run_with_model_cls(self, model_cls: Type[Model]) -> None:
        auto_field = model_cls._meta.auto_field
        if auto_field and not self.model_classdef.info.has_readable_member(auto_field.attname):
            # autogenerated field
            auto_field_fullname = helpers.get_class_fullname(auto_field.__class__)
            auto_field_info = self.lookup_typeinfo_or_incomplete_defn_error(auto_field_fullname)

            set_type, get_type = fields.get_field_descriptor_types(
                auto_field_info, is_set_nullable=True, is_get_nullable=False
            )
            self.add_new_node_to_model_class(auto_field.attname, Instance(auto_field_info, [set_type, get_type]))


class AddRelatedModelsId(ModelClassInitializer):
    def run_with_model_cls(self, model_cls: Type[Model]) -> None:
        for field in model_cls._meta.get_fields():
            if isinstance(field, ForeignKey):
                related_model_cls = self.django_context.get_field_related_model_cls(field)
                if related_model_cls is None:
                    error_context: Context = self.ctx.cls
                    field_sym = self.ctx.cls.info.get(field.name)
                    if field_sym is not None and field_sym.node is not None:
                        error_context = field_sym.node
                    self.api.fail(
                        f"Cannot find model {field.related_model!r} " f"referenced in field {field.name!r} ",
                        ctx=error_context,
                    )
                    self.add_new_node_to_model_class(field.attname, AnyType(TypeOfAny.explicit))
                    continue

                if related_model_cls._meta.abstract:
                    continue

                rel_primary_key_field = self.django_context.get_primary_key_field(related_model_cls)
                try:
                    field_info = self.lookup_class_typeinfo_or_incomplete_defn_error(rel_primary_key_field.__class__)
                except helpers.IncompleteDefnException as exc:
                    if not self.api.final_iteration:
                        raise exc
                    else:
                        continue

                is_nullable = self.django_context.get_field_nullability(field, None)
                set_type, get_type = get_field_descriptor_types(
                    field_info, is_set_nullable=is_nullable, is_get_nullable=is_nullable
                )
                self.add_new_node_to_model_class(field.attname, Instance(field_info, [set_type, get_type]))


class AddManagers(ModelClassInitializer):
    def has_any_parametrized_manager_as_base(self, info: TypeInfo) -> bool:
        for base in helpers.iter_bases(info):
            if self.is_any_parametrized_manager(base):
                return True
        return False

    def is_any_parametrized_manager(self, typ: Instance) -> bool:
        return typ.type.fullname in fullnames.MANAGER_CLASSES and isinstance(typ.args[0], AnyType)

    def get_generated_manager_mappings(self, base_manager_fullname: str) -> Dict[str, str]:
        base_manager_info = self.lookup_typeinfo(base_manager_fullname)
        if base_manager_info is None or "from_queryset_managers" not in base_manager_info.metadata:
            return {}
        return base_manager_info.metadata["from_queryset_managers"]

    def create_new_model_parametrized_manager(self, name: str, base_manager_info: TypeInfo) -> Instance:
        bases = []
        for original_base in base_manager_info.bases:
            if self.is_any_parametrized_manager(original_base):
                if original_base.type is None:
                    raise helpers.IncompleteDefnException()

                original_base = helpers.reparametrize_instance(original_base, [Instance(self.model_classdef.info, [])])
            bases.append(original_base)

        new_manager_info = self.add_new_class_for_current_module(name, bases)
        # copy fields to a new manager
        new_cls_def_context = ClassDefContext(cls=new_manager_info.defn, reason=self.ctx.reason, api=self.api)
        custom_manager_type = Instance(new_manager_info, [Instance(self.model_classdef.info, [])])

        for name, sym in base_manager_info.names.items():
            # replace self type with new class, if copying method
            if isinstance(sym.node, FuncDef):
                helpers.copy_method_to_another_class(
                    new_cls_def_context, self_type=custom_manager_type, new_method_name=name, method_node=sym.node
                )
                continue

            new_sym = sym.copy()
            if isinstance(new_sym.node, Var):
                new_var = Var(name, type=sym.type)
                new_var.info = new_manager_info
                new_var._fullname = new_manager_info.fullname + "." + name
                new_sym.node = new_var
            new_manager_info.names[name] = new_sym

        return custom_manager_type

    def run_with_model_cls(self, model_cls: Type[Model]) -> None:
        manager_info: Optional[TypeInfo]

        for manager_name, manager in model_cls._meta.managers_map.items():
            manager_class_name = manager.__class__.__name__
            manager_fullname = helpers.get_class_fullname(manager.__class__)
            try:
                manager_info = self.lookup_typeinfo_or_incomplete_defn_error(manager_fullname)
            except helpers.IncompleteDefnException as exc:
                if not self.api.final_iteration:
                    raise exc
                else:
                    base_manager_fullname = helpers.get_class_fullname(manager.__class__.__bases__[0])
                    generated_managers = self.get_generated_manager_mappings(base_manager_fullname)
                    if manager_fullname not in generated_managers:
                        # not a generated manager, continue with the loop
                        continue
                    real_manager_fullname = generated_managers[manager_fullname]
                    manager_info = self.lookup_typeinfo(real_manager_fullname)
                    if manager_info is None:
                        continue
                    manager_class_name = real_manager_fullname.rsplit(".", maxsplit=1)[1]

            if manager_name not in self.model_classdef.info.names:
                manager_type = Instance(manager_info, [Instance(self.model_classdef.info, [])])
                self.add_new_node_to_model_class(manager_name, manager_type)
            else:
                # creates new MODELNAME_MANAGERCLASSNAME class that represents manager parametrized with current model
                if not self.has_any_parametrized_manager_as_base(manager_info):
                    continue

                custom_model_manager_name = manager.model.__name__ + "_" + manager_class_name
                try:
                    custom_manager_type = self.create_new_model_parametrized_manager(
                        custom_model_manager_name, base_manager_info=manager_info
                    )
                except helpers.IncompleteDefnException:
                    continue

                self.add_new_node_to_model_class(manager_name, custom_manager_type)


class AddDefaultManagerAttribute(ModelClassInitializer):
    def run_with_model_cls(self, model_cls: Type[Model]) -> None:
        # add _default_manager
        if "_default_manager" not in self.model_classdef.info.names:
            default_manager_fullname = helpers.get_class_fullname(model_cls._meta.default_manager.__class__)
            default_manager_info = self.lookup_typeinfo_or_incomplete_defn_error(default_manager_fullname)
            default_manager = Instance(default_manager_info, [Instance(self.model_classdef.info, [])])
            self.add_new_node_to_model_class("_default_manager", default_manager)


class AddRelatedManagers(ModelClassInitializer):
    def run_with_model_cls(self, model_cls: Type[Model]) -> None:
        # add related managers
        for relation in self.django_context.get_model_relations(model_cls):
            attname = relation.get_accessor_name()
            if attname is None:
                # no reverse accessor
                continue

            related_model_cls = self.django_context.get_field_related_model_cls(relation)
            if related_model_cls is None:
                continue

            try:
                related_model_info = self.lookup_class_typeinfo_or_incomplete_defn_error(related_model_cls)
            except helpers.IncompleteDefnException as exc:
                if not self.api.final_iteration:
                    raise exc
                else:
                    continue

            if isinstance(relation, OneToOneRel):
                self.add_new_node_to_model_class(attname, Instance(related_model_info, []))
                continue

            if isinstance(relation, (ManyToOneRel, ManyToManyRel)):
                try:
                    related_manager_info = self.lookup_typeinfo_or_incomplete_defn_error(
                        fullnames.RELATED_MANAGER_CLASS
                    )  # noqa: E501
                    objects = related_model_info.get("objects")
                    if not objects:
                        raise helpers.IncompleteDefnException()
                except helpers.IncompleteDefnException as exc:
                    if not self.api.final_iteration:
                        raise exc
                    else:
                        continue

                # create new RelatedManager subclass
                parametrized_related_manager_type = Instance(related_manager_info, [Instance(related_model_info, [])])
                default_manager_type = objects.type
                if default_manager_type is None:
                    default_manager_type = self.try_generate_related_manager(related_model_cls, related_model_info)
                if (
                    default_manager_type is None
                    or not isinstance(default_manager_type, Instance)
                    or default_manager_type.type.fullname == fullnames.MANAGER_CLASS_FULLNAME
                ):
                    self.add_new_node_to_model_class(attname, parametrized_related_manager_type)
                    continue

                name = model_cls.__name__ + "_" + related_model_cls.__name__ + "_" + "RelatedManager"
                bases = [parametrized_related_manager_type, default_manager_type]
                new_related_manager_info = self.add_new_class_for_current_module(name, bases)

                self.add_new_node_to_model_class(attname, Instance(new_related_manager_info, []))

    def get_generated_manager_mappings(self, base_manager_fullname: str) -> Dict[str, str]:
        base_manager_info = self.lookup_typeinfo(base_manager_fullname)
        if base_manager_info is None or "from_queryset_managers" not in base_manager_info.metadata:
            return {}
        return base_manager_info.metadata["from_queryset_managers"]

    def try_generate_related_manager(
        self, related_model_cls: Type[Model], related_model_info: TypeInfo
    ) -> Optional[Instance]:
        manager = related_model_cls._meta.managers_map["objects"]
        base_manager_fullname = helpers.get_class_fullname(manager.__class__.__bases__[0])
        manager_fullname = helpers.get_class_fullname(manager.__class__)
        generated_managers = self.get_generated_manager_mappings(base_manager_fullname)
        if manager_fullname in generated_managers:
            real_manager_fullname = generated_managers[manager_fullname]
            manager_info = self.lookup_typeinfo(real_manager_fullname)
            if manager_info:
                return Instance(manager_info, [Instance(related_model_info, [])])
        return None


class AddExtraFieldMethods(ModelClassInitializer):
    def run_with_model_cls(self, model_cls: Type[Model]) -> None:
        # get_FOO_display for choices
        for field in self.django_context.get_model_fields(model_cls):
            if field.choices:
                info = self.lookup_typeinfo_or_incomplete_defn_error("builtins.str")
                return_type = Instance(info, [])
                common.add_method(self.ctx, name=f"get_{field.attname}_display", args=[], return_type=return_type)

        # get_next_by, get_previous_by for Date, DateTime
        for field in self.django_context.get_model_fields(model_cls):
            if isinstance(field, (DateField, DateTimeField)) and not field.null:
                return_type = Instance(self.model_classdef.info, [])
                common.add_method(
                    self.ctx,
                    name=f"get_next_by_{field.attname}",
                    args=[
                        Argument(
                            Var("kwargs", AnyType(TypeOfAny.explicit)),
                            AnyType(TypeOfAny.explicit),
                            initializer=None,
                            kind=ARG_STAR2,
                        )
                    ],
                    return_type=return_type,
                )
                common.add_method(
                    self.ctx,
                    name=f"get_previous_by_{field.attname}",
                    args=[
                        Argument(
                            Var("kwargs", AnyType(TypeOfAny.explicit)),
                            AnyType(TypeOfAny.explicit),
                            initializer=None,
                            kind=ARG_STAR2,
                        )
                    ],
                    return_type=return_type,
                )


class AddMetaOptionsAttribute(ModelClassInitializer):
    def run_with_model_cls(self, model_cls: Type[Model]) -> None:
        if "_meta" not in self.model_classdef.info.names:
            options_info = self.lookup_typeinfo_or_incomplete_defn_error(fullnames.OPTIONS_CLASS_FULLNAME)
            self.add_new_node_to_model_class("_meta", Instance(options_info, [Instance(self.model_classdef.info, [])]))


def process_model_class(ctx: ClassDefContext, django_context: DjangoContext) -> None:
    initializers = [
        InjectAnyAsBaseForNestedMeta,
        AddDefaultPrimaryKey,
        AddRelatedModelsId,
        AddManagers,
        AddDefaultManagerAttribute,
        AddRelatedManagers,
        AddExtraFieldMethods,
        AddMetaOptionsAttribute,
    ]
    for initializer_cls in initializers:
        try:
            initializer_cls(ctx, django_context).run()
        except helpers.IncompleteDefnException:
            if not ctx.api.final_iteration:
                ctx.api.defer()


def set_auth_user_model_boolean_fields(ctx: AttributeContext, django_context: DjangoContext) -> MypyType:
    boolinfo = helpers.lookup_class_typeinfo(helpers.get_typechecker_api(ctx), bool)
    assert boolinfo is not None
    return Instance(boolinfo, [])


def handle_annotated_type(ctx: AnalyzeTypeContext, django_context: DjangoContext) -> MypyType:
    args = ctx.type.args
    type_arg = ctx.api.analyze_type(args[0])
    api = cast(SemanticAnalyzer, ctx.api.api)  # type: ignore

    if not isinstance(type_arg, Instance):
        return ctx.api.analyze_type(ctx.type)

    fields_dict = None
    if len(args) > 1:
        second_arg_type = ctx.api.analyze_type(args[1])
        if isinstance(second_arg_type, TypedDictType):
            fields_dict = second_arg_type
        elif isinstance(second_arg_type, Instance) and second_arg_type.type.fullname == ANNOTATIONS_FULLNAME:
            annotations_type_arg = second_arg_type.args[0]
            if isinstance(annotations_type_arg, TypedDictType):
                fields_dict = annotations_type_arg
            elif not isinstance(annotations_type_arg, AnyType):
                ctx.api.fail("Only TypedDicts are supported as type arguments to Annotations", ctx.context)

    return get_or_create_annotated_type(api, type_arg, fields_dict=fields_dict)


def get_or_create_annotated_type(
    api: Union[SemanticAnalyzer, CheckerPluginInterface], model_type: Instance, fields_dict: Optional[TypedDictType]
) -> Instance:
    """

    Get or create the type for a model for which you getting/setting any attr is allowed.

    The generated type is an subclass of the model and django._AnyAttrAllowed.
    The generated type is placed in the django_stubs_ext module, with the name WithAnnotations[ModelName].
    If the user wanted to annotate their code using this type, then this is the annotation they would use.
    This is a bit of a hack to make a pretty type for error messages and which would make sense for users.
    """
    model_module_name = "django_stubs_ext"

    if helpers.is_annotated_model_fullname(model_type.type.fullname):
        # If it's already a generated class, we want to use the original model as a base
        model_type = model_type.type.bases[0]

    if fields_dict is not None:
        type_name = f"WithAnnotations[{model_type.type.fullname}, {fields_dict}]"
    else:
        type_name = f"WithAnnotations[{model_type.type.fullname}]"

    annotated_typeinfo = helpers.lookup_fully_qualified_typeinfo(
        cast(TypeChecker, api), model_module_name + "." + type_name
    )
    if annotated_typeinfo is None:
        model_module_file = api.modules[model_module_name]  # type: ignore

        if isinstance(api, SemanticAnalyzer):
            annotated_model_type = api.named_type_or_none(ANY_ATTR_ALLOWED_CLASS_FULLNAME, [])
            assert annotated_model_type is not None
        else:
            annotated_model_type = api.named_generic_type(ANY_ATTR_ALLOWED_CLASS_FULLNAME, [])

        annotated_typeinfo = add_new_class_for_module(
            model_module_file,
            type_name,
            bases=[model_type] if fields_dict is not None else [model_type, annotated_model_type],
            fields=fields_dict.items if fields_dict is not None else None,
        )
        if fields_dict is not None:
            # To allow structural subtyping, make it a Protocol
            annotated_typeinfo.is_protocol = True
            # Save for later to easily find which field types were annotated
            annotated_typeinfo.metadata["annotated_field_types"] = fields_dict.items
    annotated_type = Instance(annotated_typeinfo, [])
    return annotated_type
