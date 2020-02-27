from typing import List, Dict, Optional
from pydantic import BaseModel
from fastapi import Query
from monty.json import MSONable
from maggma.api.util import STORE_PARAMS, dynamic_import
import inspect


class QueryOperator(MSONable):
    """
    Base Query Operator class for defining powerfull query language
    in the Materials API
    """

    def query(self) -> STORE_PARAMS:
        """
        The query function that does the work for this query operator
        """
        raise NotImplementedError("Query operators must implement query")

    def meta(self) -> Dict:
        """
        Returns meta data to return with the Response
        """
        return {}

    def post_process(self, doc: Dict) -> Dict:
        """
        An optional post-processing function for the data
        """
        return doc


class PaginationQuery(QueryOperator):
    """Query opertators to provides Pagination in the Materials API"""

    def __init__(
        self, default_skip: int = 0, default_limit: int = 10, max_limit: int = 100
    ):
        """
        Args:
            default_skip: the default number of documents to skip
            default_limit: the default number of documents to return
            max_limit: max number of documents to return
        """
        self.default_skip = default_skip
        self.default_limit = default_limit
        self.max_limit = max_limit

        def query(
            skip: int = Query(
                default_skip, description="Number of entries to skip in the search"
            ),
            limit: int = Query(
                default_limit,
                description="Max number of entries to return in a single query."
                f" Limited to {max_limit}",
            ),
        ) -> STORE_PARAMS:
            """
            Pagination parameters for the API Endpoint
            """
            if limit > max_limit:
                raise Exception(
                    "Requested more data per query than allowed by this endpoint."
                    f"The max limit is {max_limit} entries"
                )
            return {"skip": skip, "limit": limit}

        self.query = query

    def meta(self) -> Dict:
        """
        Metadata for the pagination params
        """
        return {"max_limit": self.max_limit}


class SparseFieldsQuery(QueryOperator):
    """
    Factory function to generate a dependency for sparse field sets in FastAPI
    """

    def __init__(self, model: BaseModel, default_fields: Optional[List[str]] = None):
        """
        Args:
            model: PyDantic Model that represents the underlying data source
            default_fields: default fields to return in the API response if no fields are explicitly requested
        """

        self.model = model

        model_fields = list(self.model.__fields__.keys())
        # print(model_fields)
        # print(default_fields)
        self.default_fields = (
            model_fields if default_fields is None else list(default_fields)
        )

        assert set(self.default_fields).issubset(
            model_fields
        ), "default projection contains some fields that are not in the model fields"

        default_fields_string = ",".join(default_fields)

        def query(
            fields: str = Query(
                default_fields_string,
                description=f"Fields to project from {model.__name__} as a list of comma seperated strings",
            ),
            all_fields: bool = Query(False, description="Include all fields."),
        ) -> STORE_PARAMS:
            """
            Pagination parameters for the API Endpoint
            """

            fields = fields.split(",")
            if all_fields:
                fields = model_fields

            return {"properties": fields}

        self.query = query

    def meta(self) -> Dict:
        """
        Returns metadata for the Sparse field set
        """
        return {"default_fields": self.default_fields}

    def as_dict(self) -> Dict:
        """
        Special as_dict implemented to convert pydantic models into strings
        """

        d = super().as_dict()  # Ensures sub-classes serialize correctly
        d["model"] = f"{self.model.__module__}.{self.model.__name__}"
        return d

    @classmethod
    def from_dict(cls, d):

        model = d.get("model")
        if isinstance(model, str):
            module_path = ".".join(model.split(".")[:-1])
            class_name = model.split(".")[-1]
            model = dynamic_import(module_path, class_name)

        assert issubclass(
            model, BaseModel
        ), "The resource model has to be a PyDantic Model"
        d["model"] = model

        cls(**d)


class DefaultDynamicQuery(QueryOperator):
    def __init__(
        self,
        model: BaseModel,
        additional_signature_fields=None,
        supported_types: list = None,
        query_mapping: dict = None,
    ):
        self.model = model

        self.supported_types = (
            supported_types if supported_types is not None else [str, int, float]
        )

        if query_mapping is None:
            self.query_mapping = {
                "eq": "$eq",
                "not_eq": "$ne",
                "lt": "$lt",
                "gt": "$gt",
                "in": "$in",
                "not_in": "$nin",
            }
            query_mapping = self.query_mapping
        else:
            self.query_mapping = query_mapping

        self.additional_signature_fields = additional_signature_fields
        if additional_signature_fields is None:
            self.additional_signature_fields = dict()

            # construct fields
            # find all fields in data_object
        all_fields = list(model.__fields__.items())

        # turn fields into operators, also do type checking
        params = self.fields_to_operator(all_fields)

        # combine with additional_fields
        # user's input always have higher priority than the the default data model's
        params.update(self.additional_signature_fields)

        def query(**kwargs) -> STORE_PARAMS:
            crit = dict()
            for k, v in kwargs.items():
                if v is not None:
                    name, operator = k.split("_", 1)
                    try:
                        crit[name] = {query_mapping[operator]: v}
                    except KeyError:
                        raise KeyError(
                            f"Cannot find key {k} in current query to database mapping"
                        )
            return {"criteria": crit}
            # TODO ask shyam about this part, how to let it return something compatible to STORE_PARAMS

        # building the signatures for FastAPI Swagger UI
        signatures = []
        signatures.extend(
            inspect.Parameter(
                param,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=query[1],
                annotation=query[0],
            )
            for param, query in params.items()
        )
        query.__signature__ = inspect.Signature(signatures)

        self.query = query

    def fields_to_operator(self, all_fields):
        params = dict()
        for name, model_field in all_fields:
            if model_field.type_ in self.supported_types:
                params[f"{model_field.name}_eq"] = [
                    model_field.type_,
                    Query(
                        model_field.default,
                        description=f"Querying if {model_field.name} is equal to another",
                    ),
                ]
                params[f"{model_field.name}_not_eq"] = [
                    model_field.type_,
                    Query(
                        model_field.default,
                        description=f"Querying if {model_field.name} is not equal to another",
                    ),
                ]
                params[f"{model_field.name}_in"] = [
                    List[model_field.type_],
                    Query(
                        model_field.default,
                        description=f"Querying if item is in {model_field.name}",
                    ),
                ]
                params[f"{model_field.name}_not_in"] = [
                    List[model_field.type_],
                    Query(
                        model_field.default,
                        description=f"Querying if item is not in {model_field.name} ",
                    ),
                ]

                if model_field.type_ == int or model_field == float:
                    params[f"{model_field.name}_lt"] = [
                        model_field.type_,
                        Query(
                            model_field.default,
                            description=f"Querying if {model_field.name} is less than to another",
                        ),
                    ]
                    params[f"{model_field.name}_gt"] = [
                        model_field.type_,
                        Query(
                            model_field.default,
                            description=f"Querying if {model_field.name} is greater than to another",
                        ),
                    ]
            else:
                import warnings

                warnings.warn(
                    f"Field name {model_field.name} with {model_field.type_} not implemented"
                )
                # raise NotImplementedError(
                #     f"Field name {model_field.name} with {model_field.type_} not implemented"
                # )
        return params