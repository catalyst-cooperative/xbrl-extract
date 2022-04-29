"""Parse a single instance."""
from datetime import date
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

import pandas as pd
import sqlalchemy as sa
from lxml import etree  # nosec: B410
from lxml.etree import _Element as Element  # nosec: B410
from pydantic import BaseModel, validator

TAXONOMY_PREFIX = "{http://www.w3.org/1999/xlink}href"  # noqa: FS003


def parse(path: str):
    """
    Parse a single XBRL instance using XML library directly.

    Args:
        path: Path to XBRL filing.

    Returns:
        context_dict: Dictionary of contexts in filing.
        fact_dict: Dictionary of facts in filing.
        taxonomy_url: URL to taxonomy used for filing.
    """
    tree = etree.parse(path)  # nosec: B320
    root = tree.getroot()

    taxonomy_url = root.find("link:schemaRef", root.nsmap).attrib[TAXONOMY_PREFIX]

    context_dict = {}
    fact_dict = {}
    for child in root.iterchildren():
        if "id" not in child.attrib:
            continue

        if child.attrib["id"].startswith("c"):
            new_context = Context.from_xml(child)
            context_dict[new_context.c_id] = new_context
            fact_dict[new_context.c_id] = []

        elif child.attrib["id"].startswith("f"):
            new_fact = Fact.from_xml(child)
            if new_fact.value is not None:
                fact_dict[new_fact.c_id].append(new_fact)

    return context_dict, fact_dict, taxonomy_url


class XbrlDb(object):
    """Class to manage writing extracted XBRL data to SQLite database."""

    def __init__(self, engine: sa.engine.Engine, batch_size: int, num_instances: int):
        """Construct db manager."""
        self.engine = engine
        self.batch_size = batch_size
        self.num_instances = num_instances
        self.dfs = {}
        self.counter = 1

    def append_instance(self, instance: Dict[str, Tuple[pd.DataFrame, pd.DataFrame]]):
        """Append dataframes from one instance, and write to disk if batch is finished."""
        for key, (duration_dfs, instant_dfs) in instance.items():
            if key not in self.dfs:
                self.dfs[key] = {"duration": [], "instant": []}

            self.dfs[key]["duration"].append(duration_dfs)
            self.dfs[key]["instant"].append(instant_dfs)

        if (self.counter % self.batch_size == 0) or (  # noqa: FS001
            self.counter == self.num_instances
        ):
            for key, df_dict in self.dfs.items():
                duration_df = pd.concat(df_dict["duration"], ignore_index=True)
                duration_df.to_sql(f"{key} - duration", self.engine, if_exists="append")

                instant_df = pd.concat(df_dict["instant"], ignore_index=True)
                instant_df.to_sql(f"{key} - instant", self.engine, if_exists="append")

        self.counter += 1


class Period(BaseModel):
    """
    Pydantic model that defines an XBRL period.

    A period can be instantaneous or a duration of time.
    """

    instant: bool
    start_date: Optional[date]
    end_date: date

    @classmethod
    def from_xml(cls, elem: Element) -> "Period":
        """Construct Period from XML element."""
        instant = elem.find("instant", elem.nsmap)
        if instant is not None:
            return cls(instant=True, end_date=instant.text)

        return cls(
            instant=False,
            start_date=elem.find("startDate", elem.nsmap).text,
            end_date=elem.find("endDate", elem.nsmap).text,
        )


class DimensionType(Enum):
    """
    Indicate dimension type.

    XBRL contains explicit (all allowable values defined in taxonomy) and typed
    (value defined in filing) dimensions.
    """

    EXPLICIT = auto()
    TYPED = auto()


class Axis(BaseModel):
    """
    Pydantic model that defines an XBRL Axis.

    Axes (or dimensions, terms are interchangeable in XBRL) are used for identifying
    individual facts when the entity id, and period are insufficient. All axes will
    be turned into columns, and be a part of the primary key for the table they
    belong to.
    """

    name: str
    value: str
    dimension_type: DimensionType

    @validator("name", pre=True)
    def strip_prefix(cls, name):  # noqa: N805
        """Strip XML prefix from name."""
        return name.split(":")[1] if ":" in name else name

    @classmethod
    def from_xml(cls, elem: Element) -> "Axis":
        """Construct Axis from XML element."""
        if elem.tag.endswith("explicitMember"):
            return cls(
                name=elem.attrib["dimension"],
                value=elem.text,
                dimension_type=DimensionType.EXPLICIT,
            )

        elif elem.tag.endswith("typedMember"):
            dim = elem.getchildren()[0]
            return cls(
                name=elem.attrib["dimension"],
                value=dim.text,
                dimension_type=DimensionType.TYPED,
            )


class Entity(BaseModel):
    """
    Pydantic model that defines an XBRL Entity.

    Entities are used to identify individual XBRL facts. An Entity should
    contain a unique identifier, as well as any dimensions defined for a
    table.
    """

    identifier: str
    dimensions: List[Axis]

    @classmethod
    def from_xml(cls, elem: Element) -> "Entity":
        """Construct Entity from XML element."""
        # Segment node contains dimensions prefixed with xbrldi
        segment = elem.find("segment", elem.nsmap)
        dims = segment.findall("xbrldi:*", segment.nsmap) if segment is not None else []

        return cls(
            identifier=elem.find("identifier", elem.nsmap).text,
            dimensions=[Axis.from_xml(child) for child in dims],
        )

    def check_dimensions(self, axes: List[str]) -> bool:
        """Verify that fact and Entity have equivalent dimensions."""
        if len(self.dimensions) != len(axes):
            return False

        for dim in self.dimensions:
            if dim.name not in axes:
                return False
        return True


class Context(BaseModel):
    """
    Pydantic model that defines an XBRL Context.

    Contexts are used to provide useful background information for facts. The
    context indicates the entity, time period, and any other dimensions which apply
    to the fact.
    """

    c_id: str
    entity: Entity
    period: Period

    @classmethod
    def from_xml(cls, elem: Element) -> "Context":
        """Construct Context from XML element."""
        return cls(
            **{
                "c_id": elem.attrib["id"],
                "entity": Entity.from_xml(elem.find("entity", elem.nsmap)),
                "period": Period.from_xml(elem.find("period", elem.nsmap)),
            }
        )

    def check_dimensions(self, axes: List[str]) -> bool:
        """
        Helper function to check if a context falls within axes.

        Args:
            axes: List of axes to verify against.
        """
        return self.entity.check_dimensions(axes)

    def get_context_ids(self, filing_name: str) -> Dict:
        """Return a dictionary that defines context."""
        axes_dict = {axis.name: axis.value for axis in self.entity.dimensions}

        # Get date based on period type
        if self.period.instant:
            date_dict = {"date": self.period.end_date}
        else:
            date_dict = {
                "start_date": self.period.start_date,
                "end_date": self.period.end_date,
            }

        return {
            "context_id": self.c_id,
            "entity_id": self.entity.identifier,
            "filing_name": filing_name,
            **date_dict,
            **axes_dict,
        }


class Fact(BaseModel):
    """
    Pydantic model that defines an XBRL Fact.

    A fact is a single "data point", which contains a name, value, and a Context to
    give background information.
    """

    name: str
    c_id: str
    value: Optional[str]

    @classmethod
    def from_xml(cls, elem: Element) -> "Fact":
        """Construct Fact from XML element."""
        # Get prefix from namespace map to strip from fact name
        prefix = f"{{{elem.nsmap[elem.prefix]}}}"
        return cls(
            name=elem.tag.replace(prefix, ""),  # Strip prefix
            c_id=elem.attrib["contextRef"],
            value=elem.text,
        )
