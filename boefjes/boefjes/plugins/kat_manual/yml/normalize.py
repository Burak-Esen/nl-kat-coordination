import yaml
import io
import logging
from collections.abc import Iterable
from typing import Any, TypedDict, NotRequired
from functools import reduce

from pydantic import ValidationError

from boefjes.job_models import NormalizerDeclaration, NormalizerOutput
from octopoes.models import OOI, Reference
from models.ooi.findings import Finding
from octopoes.models.ooi.geography import GeographicPoint
from octopoes.models.ooi.network import Network
from octopoes.models.ooi.web import WebURL

class OOITypeEntry(TypedDict):
    type: Any
    distinctive_fields: NotRequired[list[str]]

def get_subclasses_deep(cls, res=[], sub_clss=[]):
    if not len(sub_clss):
        sub_clss = cls.__subclasses__()
    for s_cls in sub_clss:
        if s_cls not in res: res.append(s_cls)
        new_sub_clss = s_cls.__subclasses__()
        if len(new_sub_clss):
            get_subclasses_deep(s_cls, res, new_sub_clss)
    return res

OOI_TYPES: dict[str, OOITypeEntry] = reduce(
        lambda ooit, dic: ooit.update(dic) or ooit,
        map(
            lambda cls: { cls.__name__: { "type": cls } },
            get_subclasses_deep(OOI)
        ),
        {}
    )
# Types without _natural_key_attrs
OOI_TYPES["GeographicPoint"] = {"type": GeographicPoint, "distinctive_fields": ["ooi", "longitude", "latitude"]}
OOI_TYPES["Finding"] = {"type": Finding, "distinctive_fields": ["ooi", "finding_type"]}
OOI_TYPES["WebURL"] = {"type": WebURL, "distinctive_fields": ["scheme", "port", "path"]}


logger = logging.getLogger(__name__)


def run(input_ooi: dict, raw: bytes) -> Iterable[NormalizerOutput]:
    reference_cache = {"Network": {"internet": Network(name="internet")}}

    yield from process_yml(raw, reference_cache)


def process_yml(yml_raw_data: bytes, reference_cache: dict) -> Iterable[NormalizerOutput]:
    yml_data = io.StringIO(yml_raw_data.decode("UTF-8"))
    oois_from_yaml = yaml.safe_load(yml_data)
    oois = []
    for ooi_number, ooi_dict in enumerate(oois_from_yaml, start=1):
        try:
            create_oois(ooi_dict, reference_cache, oois)
        except ValidationError as err:
            logger.exception("Validation failed for indexed object at %s", ooi_number)
            logger.exception(f"with error: {str(err)}")
    return oois

def get_distinctive_fields(ooi_type):
    return OOI_TYPES[ooi_type.__name__].get('distinctive_fields', ooi_type._natural_key_attrs)

def create_oois(ooi_dict:dict, reference_cache:dict, oois_list:list):
    # constants
    skip_properties = ("object_type", "scan_profile", "primary_key", "user_id")
    # check for main ooi
    ooi_type = OOI_TYPES[ooi_dict["ooi_type"]]["type"]
    # Special Cases
    if hasattr(ooi_type, 'type_from_raw'): ooi_type = ooi_type.type_from_raw(ooi_dict)
    # check for cache
    cache, cache_field_name = get_cache_and_field_name(ooi_type, ooi_dict, reference_cache)
    if cache_field_name in cache: return cache[cache_field_name]
    # creation process
    ooi_fields = [
        (field, field if model_field.annotation != Reference else model_field.json_schema_extra['object_type'], model_field.annotation == Reference, model_field.is_required())
        for field, model_field in ooi_type.__fields__.items()
        if field not in skip_properties
    ]
    kwargs: dict[str, Any] = {}
    for field, referenced_type, is_reference, required in ooi_fields:
        # required referenced fields or not required but also defined in yaml
        if is_reference and required or is_reference and ooi_dict.get(field):
            try:
                referenced_ooi = create_oois(
                    ooi_dict.get(field.lower()) or ooi_dict.get(referenced_type.lower()),
                    reference_cache,
                    oois_list
                )
                kwargs[field] = referenced_ooi.reference
            except IndexError:
                if required:
                    raise IndexError(
                        f"Required referenced primary-key field '{field}' not set "
                        f"and no default present for Type '{ooi_type.__name__}'."
                    )
                else:
                    kwargs[field] = None
        # not required and not defined referenced field still in loop. they skipped with "not is_reference"
        # required feilds or not required but also defined in yaml
        elif not is_reference and (required or not required and ooi_dict.get(field)):
            kwargs[field] = ooi_dict.get(field)
    ooi = ooi_type(**kwargs)
    # Save to cache
    cache[cache_field_name] = ooi
    oois_list.append(NormalizerDeclaration(ooi=ooi))
    return ooi
    
def get_cache_and_field_name(ooi_type, ooi_dict: dict, reference_cache:dict):
    dins_fields = get_distinctive_fields(ooi_type)
    cache_field_name = get_cache_name(ooi_dict, dins_fields)
    cache = reference_cache.setdefault(ooi_type.__name__, {})
    return cache, cache_field_name

def get_cache_name(ooi_dict:dict, field_combination: list[str]):
    """It creates name for cache from str values of distinctive fields"""
    return "|".join(filter(None, map(lambda a: str(ooi_dict.get(a, "")), field_combination)))
