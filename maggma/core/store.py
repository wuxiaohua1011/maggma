# coding: utf-8
"""
Module containing the core Store definition
"""
from __future__ import annotations

import logging


from abc import ABCMeta, abstractmethod, abstractproperty

from datetime import datetime
from enum import Enum
from typing import Union, Optional, Dict, List, Iterator, Tuple

from pydash import identity

from monty.dev import deprecated
from monty.json import MSONable, MontyDecoder
from maggma.utils import source_keys_updated, LU_KEY_ISOFORMAT
from maggma.core import Validator


class Sort(Enum):
    Ascending = 1
    Descending = 2


class DateTimeFormat(Enum):
    DateTime = "datetime"
    IsoFormat = "isoformat"


class Store(MSONable, metaclass=ABCMeta):
    """
    Abstract class for a data Store
    Defines the interface for all data going in and out of a Builder
    """

    def __init__(
        self,
        key: str = "task_id",
        last_updated_field: str = "last_updated",
        last_updated_type: DateTimeFormat = "datetime",
        validator: Optional[Validator] = None,
    ):
        """
        Args:
            key : master key to index on
            last_updated_field : field for date/time stamping the data
            last_updated_type : the date/time format for the last_updated_field.
                                Can be "datetime" or "isoformat"
            validator : Validator to validate documents going into the store
        """
        self.key = key
        self.last_updated_field = last_updated_field
        self.last_updated_type = last_updated_type
        self._lu_func = (
            LU_KEY_ISOFORMAT
            if last_updated_type == DateTimeFormat.IsoFormat
            else (identity, identity)
        )
        self.validator = validator
        self.logger = logging.getLogger(type(self).__name__)
        self.logger.addHandler(logging.NullHandler())

    @abstractproperty
    @deprecated(message="This will be removed in the future")
    def collection(self):
        """
        Returns a handle to the pymongo collection object
        Not guaranteed to exist in the future
        """
        pass

    @abstractmethod
    def connect(self, force_reset: bool = False):
        """
        Connect to the source data
        """
        pass

    @abstractmethod
    def close(self):
        """
        Closes any connections
        """
        pass

    @abstractmethod
    def query(
        self,
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Sort]] = None,
        skip: int = 0,
        limit: int = 0,
    ) -> Iterator[Dict]:
        """
        Queries the Store for a set of documents

        Args:
            criteria : PyMongo filter for documents to search in
            properties: properties to return in grouped documents
            sort: Dictionary of sort order for fields
            skip: number documents to skip
            limit: limit on total number of documents returned
        """
        pass

    def query_one(self, criteria=None, properties=None, **kwargs):
        """
        Function that gets a single document from GridFS. This store
        ignores all property projections as its designed for whole
        document access

        Args:
            criteria (dict): filter for query, matches documents
                against key-value pairs
            properties (list or dict): This will be ignored by the GridFS
                Store
            **kwargs (kwargs): further kwargs to Collection.find
        """
        return next(self.query(criteria=criteria, **kwargs), None)

    def distinct(
        self,
        field: Union[List[str], str],
        criteria: Optional[Dict] = None,
        all_exist: bool = False,
    ) -> List:
        """
        Get all distinct values for a key

        Args:
            field: the field(s) to get distinct values for
            criteria : PyMongo filter for documents to search in
            all_exist : ensure all fields exist for the distinct set
        """
        field = field if isinstance(field, list) else [field]

        criteria = criteria or {}

        if all_exist:
            criteria.update({f: {"$exists": 1} for f in field if f not in criteria})
        results = [
            key for key, _ in self.groupby(field, properties=field, criteria=criteria)
        ]
        return results

    @abstractmethod
    def update(self, docs: Union[List[Dict], Dict], key: Union[List, str, None] = None):
        """
        Update documents into the Store

        Args:
            docs: the document or list of documents to update
            key: field name(s) to determine uniqueness for a
                 document, can be a list of multiple fields,
                 a single field, or None if the Store's key
                 field is to be used
        """
        pass

    @abstractmethod
    def ensure_index(self, key: str, unique: Optional[bool] = False) -> bool:
        """
        Tries to create an index and return true if it suceeded
        Args:
            key: single key to index
            unique: Whether or not this index contains only unique keys

        Returns:
            bool indicating if the index exists/was created
        """
        pass

    @abstractmethod
    def groupby(
        self,
        keys: Union[List[str], str],
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Sort]] = None,
        skip: int = 0,
        limit: int = 0,
    ) -> Iterator[Tuple[Dict, List[Dict]]]:
        """
        Simple grouping function that will group documents
        by keys.

        Args:
            keys: fields to group documents
            criteria : PyMongo filter for documents to search in
            properties: properties to return in grouped documents
            sort: Dictionary of sort order for fields
            skip: number documents to skip
            limit: limit on total number of documents returned

        Returns:
            generator returning tuples of (key, list of docs)
        """
        pass

    @property
    def last_updated(self):
        """
        Provides the most recent last_updated date time stamp from
        the documents in this Store
        """
        doc = next(
            self.query(
                properties=[self.last_updated_field],
                sort={self.last_updated_field: Sort.Descending},
                limit=1,
            ),
            None,
        )
        if doc and self.last_updated_field not in doc:
            raise StoreError(
                f"No field '{self.last_updated_field}' in store document. Please ensure Store.last_updated_field "
                "is a datetime field in your store that represents the time of "
                "last update to each document."
            )
        # Handle when collection has docs but `NoneType` last_updated_field.
        return (
            self._lu_func[0](doc[self.last_updated_field])
            if (doc and doc[self.last_updated_field])
            else datetime.min
        )

    def newer_in(
        self,
        target: Store,
        key: Union[str, None] = None,
        criteria: Optional[Dict] = None,
        exhaustive: bool = False,
    ) -> List[str]:
        """
        Returns the keys of documents that are newer in the target
        Store than this Store.

        Args:
            key: a single key field to return, defaults to Store.key
            criteria : PyMongo filter for documents to search in
            exhaustive: triggers an item-by-item check vs. checking
                        the last_updated of the target Store and using
                        that to filter out new items in
        """
        self.ensure_index(self.key)
        self.ensure_index(self.last_updated_field)
        if exhaustive:
            return source_keys_updated(target, self, query=criteria)
        else:
            key = key if key is not None else self.key  # Default value
            criteria = {
                self.last_updated_field: {"$gt": self._lu_func[1](self.last_updated)}
            }
            return target.distinct(field=key, criteria=criteria)

    @deprecated(message="Please use Store.newer_in")
    def lu_filter(self, targets):
        """Creates a MongoDB filter for new documents.

        By "new", we mean documents in this Store that were last updated later
        than any document in targets.

        Args:
            targets (list): A list of Stores

        """
        if isinstance(targets, Store):
            targets = [targets]

        lu_list = [t.last_updated for t in targets]
        return {self.last_updated_field: {"$gt": self._lu_func[1](max(lu_list))}}

    @deprecated(message="Use Store.newer_in")
    def updated_keys(self, target, criteria=None):
        """
        Returns keys for docs that are newer in the target store in comparison
        with this store when comparing the last updated field (last_updated_field)

        Args:
            target (Store): store to look for updated documents
            criteria (dict): mongo query to limit scope

        Returns:
            list of keys that have been updated in target store
        """
        self.ensure_index(self.key)
        self.ensure_index(self.last_updated_field)

        return source_keys_updated(target, self, query=criteria)

    def __eq__(self, other):
        return hash(self) == hash(other)

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((self.last_updated_field,))

    def __getstate__(self):
        return self.as_dict()

    def __setstate__(self, d):
        d = {k: v for k, v in d.items() if not k.startswith("@")}
        d = MontyDecoder().process_decoded(d)
        self.__init__(**d)


class StoreError(Exception):
    """General Store-related error."""

    pass