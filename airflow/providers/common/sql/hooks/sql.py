# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import contextlib
import warnings
from contextlib import closing
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generator,
    Iterable,
    List,
    Mapping,
    Protocol,
    Sequence,
    TypeVar,
    cast,
    overload,
)
from urllib.parse import urlparse

import sqlparse
from sqlalchemy import create_engine

from airflow.exceptions import AirflowException, AirflowProviderDeprecationWarning
from airflow.hooks.base import BaseHook

if TYPE_CHECKING:
    from pandas import DataFrame

    from airflow.providers.openlineage.extractors import OperatorLineage
    from airflow.providers.openlineage.sqlparser import DatabaseInfo


T = TypeVar("T")


def return_single_query_results(sql: str | Iterable[str], return_last: bool, split_statements: bool):
    """
    Determine when results of single query only should be returned.

    For compatibility reasons, the behaviour of the DBAPIHook is somewhat confusing.
    In some cases, when multiple queries are run, the return value will be an iterable (list) of results
    -- one for each query. However, in other cases, when single query is run, the return value will be just
    the result of that single query without wrapping the results in a list.

    The cases when single query results are returned without wrapping them in a list are as follows:

    a) sql is string and ``return_last`` is True (regardless what ``split_statements`` value is)
    b) sql is string and ``split_statements`` is False

    In all other cases, the results are wrapped in a list, even if there is only one statement to process.
    In particular, the return value will be a list of query results in the following circumstances:

    a) when ``sql`` is an iterable of string statements (regardless what ``return_last`` value is)
    b) when ``sql`` is string, ``split_statements`` is True and ``return_last`` is False

    :param sql: sql to run (either string or list of strings)
    :param return_last: whether last statement output should only be returned
    :param split_statements: whether to split string statements.
    :return: True if the hook should return single query results
    """
    return isinstance(sql, str) and (return_last or not split_statements)


def fetch_all_handler(cursor) -> list[tuple] | None:
    """Return results for DbApiHook.run()."""
    if not hasattr(cursor, "description"):
        raise RuntimeError(
            "The database we interact with does not support DBAPI 2.0. Use operator and "
            "handlers that are specifically designed for your database."
        )
    if cursor.description is not None:
        return cursor.fetchall()
    else:
        return None


def fetch_one_handler(cursor) -> list[tuple] | None:
    """Return first result for DbApiHook.run()."""
    if not hasattr(cursor, "description"):
        raise RuntimeError(
            "The database we interact with does not support DBAPI 2.0. Use operator and "
            "handlers that are specifically designed for your database."
        )
    if cursor.description is not None:
        return cursor.fetchone()
    else:
        return None


class ConnectorProtocol(Protocol):
    """Database connection protocol."""

    def connect(self, host: str, port: int, username: str, schema: str) -> Any:
        """
        Connect to a database.

        :param host: The database host to connect to.
        :param port: The database port to connect to.
        :param username: The database username used for the authentication.
        :param schema: The database schema to connect to.
        :return: the authorized connection object.
        """


class DbApiHook(BaseHook):
    """
    Abstract base class for sql hooks.

    When subclassing, maintainers can override the `_make_common_data_structure` method:
    This method transforms the result of the handler method (typically `cursor.fetchall()`) into
    objects common across all Hooks derived from this class (tuples). Most of the time, the underlying SQL
    library already returns tuples from its cursor, and the `_make_common_data_structure` method can be ignored.

    :param schema: Optional DB schema that overrides the schema specified in the connection. Make sure that
        if you change the schema parameter value in the constructor of the derived Hook, such change
        should be done before calling the ``DBApiHook.__init__()``.
    :param log_sql: Whether to log SQL query when it's executed. Defaults to *True*.
    """

    # Override to provide the connection name.
    conn_name_attr: str
    # Override to have a default connection id for a particular dbHook
    default_conn_name = "default_conn_id"
    # Override if this db supports autocommit.
    supports_autocommit = False
    # Override with the object that exposes the connect method
    connector: ConnectorProtocol | None = None
    # Override with db-specific query to check connection
    _test_connection_sql = "select 1"

    def __init__(self, *args, schema: str | None = None, log_sql: bool = True, **kwargs):
        super().__init__(logger_name=kwargs.pop("logger_name", None))
        if not self.conn_name_attr:
            raise AirflowException("conn_name_attr is not defined")
        elif len(args) == 1:
            setattr(self, self.conn_name_attr, args[0])
        elif self.conn_name_attr not in kwargs:
            setattr(self, self.conn_name_attr, self.default_conn_name)
        else:
            setattr(self, self.conn_name_attr, kwargs[self.conn_name_attr])
        # We should not make schema available in deriving hooks for backwards compatibility
        # If a hook deriving from DBApiHook has a need to access schema, then it should retrieve it
        # from kwargs and store it on its own. We do not run "pop" here as we want to give the
        # Hook deriving from the DBApiHook to still have access to the field in its constructor
        self.__schema = schema
        self.log_sql = log_sql
        self.descriptions: list[Sequence[Sequence] | None] = []
        self._placeholder: str = "%s"

    @property
    def placeholder(self) -> str:
        return self._placeholder

    def get_conn(self):
        """Return a connection object."""
        db = self.get_connection(getattr(self, cast(str, self.conn_name_attr)))
        return self.connector.connect(host=db.host, port=db.port, username=db.login, schema=db.schema)

    def get_uri(self) -> str:
        """
        Extract the URI from the connection.

        :return: the extracted uri.
        """
        conn = self.get_connection(getattr(self, self.conn_name_attr))
        conn.schema = self.__schema or conn.schema
        return conn.get_uri()

    def get_sqlalchemy_engine(self, engine_kwargs=None):
        """
        Get an sqlalchemy_engine object.

        :param engine_kwargs: Kwargs used in :func:`~sqlalchemy.create_engine`.
        :return: the created engine.
        """
        if engine_kwargs is None:
            engine_kwargs = {}
        return create_engine(self.get_uri(), **engine_kwargs)

    def get_pandas_df(
        self,
        sql,
        parameters: list | tuple | Mapping[str, Any] | None = None,
        **kwargs,
    ) -> DataFrame:
        """
        Execute the sql and returns a pandas dataframe.

        :param sql: the sql statement to be executed (str) or a list of sql statements to execute
        :param parameters: The parameters to render the SQL query with.
        :param kwargs: (optional) passed into pandas.io.sql.read_sql method
        """
        try:
            from pandas.io import sql as psql
        except ImportError:
            raise Exception(
                "pandas library not installed, run: pip install "
                "'apache-airflow-providers-common-sql[pandas]'."
            )

        with closing(self.get_conn()) as conn:
            return psql.read_sql(sql, con=conn, params=parameters, **kwargs)

    def get_pandas_df_by_chunks(
        self,
        sql,
        parameters: list | tuple | Mapping[str, Any] | None = None,
        *,
        chunksize: int,
        **kwargs,
    ) -> Generator[DataFrame, None, None]:
        """
        Execute the sql and return a generator.

        :param sql: the sql statement to be executed (str) or a list of sql statements to execute
        :param parameters: The parameters to render the SQL query with
        :param chunksize: number of rows to include in each chunk
        :param kwargs: (optional) passed into pandas.io.sql.read_sql method
        """
        try:
            from pandas.io import sql as psql
        except ImportError:
            raise Exception(
                "pandas library not installed, run: pip install "
                "'apache-airflow-providers-common-sql[pandas]'."
            )

        with closing(self.get_conn()) as conn:
            yield from psql.read_sql(sql, con=conn, params=parameters, chunksize=chunksize, **kwargs)

    def get_records(
        self,
        sql: str | list[str],
        parameters: Iterable | Mapping[str, Any] | None = None,
    ) -> Any:
        """
        Execute the sql and return a set of records.

        :param sql: the sql statement to be executed (str) or a list of sql statements to execute
        :param parameters: The parameters to render the SQL query with.
        """
        return self.run(sql=sql, parameters=parameters, handler=fetch_all_handler)

    def get_first(self, sql: str | list[str], parameters: Iterable | Mapping[str, Any] | None = None) -> Any:
        """
        Execute the sql and return the first resulting row.

        :param sql: the sql statement to be executed (str) or a list of sql statements to execute
        :param parameters: The parameters to render the SQL query with.
        """
        return self.run(sql=sql, parameters=parameters, handler=fetch_one_handler)

    @staticmethod
    def strip_sql_string(sql: str) -> str:
        return sql.strip().rstrip(";")

    @staticmethod
    def split_sql_string(sql: str) -> list[str]:
        """
        Split string into multiple SQL expressions.

        :param sql: SQL string potentially consisting of multiple expressions
        :return: list of individual expressions
        """
        splits = sqlparse.split(sqlparse.format(sql, strip_comments=True))
        return [s for s in splits if s]

    @property
    def last_description(self) -> Sequence[Sequence] | None:
        if not self.descriptions:
            return None
        return self.descriptions[-1]

    @overload
    def run(
        self,
        sql: str | Iterable[str],
        autocommit: bool = ...,
        parameters: Iterable | Mapping[str, Any] | None = ...,
        handler: None = ...,
        split_statements: bool = ...,
        return_last: bool = ...,
    ) -> None:
        ...

    @overload
    def run(
        self,
        sql: str | Iterable[str],
        autocommit: bool = ...,
        parameters: Iterable | Mapping[str, Any] | None = ...,
        handler: Callable[[Any], T] = ...,
        split_statements: bool = ...,
        return_last: bool = ...,
    ) -> tuple | list[tuple] | list[list[tuple] | tuple] | None:
        ...

    def run(
        self,
        sql: str | Iterable[str],
        autocommit: bool = False,
        parameters: Iterable | Mapping[str, Any] | None = None,
        handler: Callable[[Any], T] | None = None,
        split_statements: bool = False,
        return_last: bool = True,
    ) -> tuple | list[tuple] | list[list[tuple] | tuple] | None:
        """Run a command or a list of commands.

        Pass a list of SQL statements to the sql parameter to get them to
        execute sequentially.

        The method will return either single query results (typically list of rows) or list of those results
        where each element in the list are results of one of the queries (typically list of list of rows :D)

        For compatibility reasons, the behaviour of the DBAPIHook is somewhat confusing.
        In some cases, when multiple queries are run, the return value will be an iterable (list) of results
        -- one for each query. However, in other cases, when single query is run, the return value will
        be the result of that single query without wrapping the results in a list.

        The cases when single query results are returned without wrapping them in a list are as follows:

        a) sql is string and ``return_last`` is True (regardless what ``split_statements`` value is)
        b) sql is string and ``split_statements`` is False

        In all other cases, the results are wrapped in a list, even if there is only one statement to process.
        In particular, the return value will be a list of query results in the following circumstances:

        a) when ``sql`` is an iterable of string statements (regardless what ``return_last`` value is)
        b) when ``sql`` is string, ``split_statements`` is True and ``return_last`` is False

        After ``run`` is called, you may access the following properties on the hook object:

        * ``descriptions``: an array of cursor descriptions. If ``return_last`` is True, this will be
          a one-element array containing the cursor ``description`` for the last statement.
          Otherwise, it will contain the cursor description for each statement executed.
        * ``last_description``: the description for the last statement executed

        Note that query result will ONLY be actually returned when a handler is provided; if
        ``handler`` is None, this method will return None.

        Handler is a way to process the rows from cursor (Iterator) into a value that is suitable to be
        returned to XCom and generally fit in memory.

        You can use pre-defined handles (``fetch_all_handler``, ``fetch_one_handler``) or implement your
        own handler.

        :param sql: the sql statement to be executed (str) or a list of
            sql statements to execute
        :param autocommit: What to set the connection's autocommit setting to
            before executing the query.
        :param parameters: The parameters to render the SQL query with.
        :param handler: The result handler which is called with the result of each statement.
        :param split_statements: Whether to split a single SQL string into statements and run separately
        :param return_last: Whether to return result for only last statement or for all after split
        :return: if handler provided, returns query results (may be list of results depending on params)
        """
        self.descriptions = []

        if isinstance(sql, str):
            if split_statements:
                sql_list: Iterable[str] = self.split_sql_string(sql)
            else:
                sql_list = [sql] if sql.strip() else []
        else:
            sql_list = sql

        if sql_list:
            self.log.debug("Executing following statements against DB: %s", sql_list)
        else:
            raise ValueError("List of SQL statements is empty")
        _last_result = None
        with closing(self.get_conn()) as conn:
            if self.supports_autocommit:
                self.set_autocommit(conn, autocommit)

            with closing(conn.cursor()) as cur:
                results = []
                for sql_statement in sql_list:
                    self._run_command(cur, sql_statement, parameters)

                    if handler is not None:
                        result = self._make_common_data_structure(handler(cur))
                        if return_single_query_results(sql, return_last, split_statements):
                            _last_result = result
                            _last_description = cur.description
                        else:
                            results.append(result)
                            self.descriptions.append(cur.description)

            # If autocommit was set to False or db does not support autocommit, we do a manual commit.
            if not self.get_autocommit(conn):
                conn.commit()

        if handler is None:
            return None
        if return_single_query_results(sql, return_last, split_statements):
            self.descriptions = [_last_description]
            return _last_result
        else:
            return results

    def _make_common_data_structure(self, result: T | Sequence[T]) -> tuple | list[tuple]:
        """Ensure the data returned from an SQL command is a standard tuple or list[tuple].

        This method is intended to be overridden by subclasses of the `DbApiHook`. Its purpose is to
        transform the result of an SQL command (typically returned by cursor methods) into a common
        data structure (a tuple or list[tuple]) across all DBApiHook derived Hooks, as defined in the
        ADR-0002 of the sql provider.

        If this method is not overridden, the result data is returned as-is. If the output of the cursor
        is already a common data structure, this method should be ignored.
        """
        # Back-compatibility call for providers implementing old ´_make_serializable' method.
        with contextlib.suppress(AttributeError):
            result = self._make_serializable(result=result)  # type: ignore[attr-defined]
            warnings.warn(
                "The `_make_serializable` method is deprecated and support will be removed in a future "
                f"version of the common.sql provider. Please update the {self.__class__.__name__}'s provider "
                "to a version based on common.sql >= 1.9.1.",
                AirflowProviderDeprecationWarning,
                stacklevel=2,
            )

        if isinstance(result, Sequence):
            return cast(List[tuple], result)
        return cast(tuple, result)

    def _run_command(self, cur, sql_statement, parameters):
        """Run a statement using an already open cursor."""
        if self.log_sql:
            self.log.info("Running statement: %s, parameters: %s", sql_statement, parameters)

        if parameters:
            cur.execute(sql_statement, parameters)
        else:
            cur.execute(sql_statement)

        # According to PEP 249, this is -1 when query result is not applicable.
        if cur.rowcount >= 0:
            self.log.info("Rows affected: %s", cur.rowcount)

    def set_autocommit(self, conn, autocommit):
        """Set the autocommit flag on the connection."""
        if not self.supports_autocommit and autocommit:
            self.log.warning(
                "%s connection doesn't support autocommit but autocommit activated.",
                getattr(self, self.conn_name_attr),
            )
        conn.autocommit = autocommit

    def get_autocommit(self, conn) -> bool:
        """Get autocommit setting for the provided connection.

        :param conn: Connection to get autocommit setting from.
        :return: connection autocommit setting. True if ``autocommit`` is set
            to True on the connection. False if it is either not set, set to
            False, or the connection does not support auto-commit.
        """
        return getattr(conn, "autocommit", False) and self.supports_autocommit

    def get_cursor(self):
        """Return a cursor."""
        return self.get_conn().cursor()

    def _generate_insert_sql(self, table, values, target_fields, replace, **kwargs) -> str:
        """
        Generate the INSERT SQL statement.

        The REPLACE variant is specific to MySQL syntax.

        :param table: Name of the target table
        :param values: The row to insert into the table
        :param target_fields: The names of the columns to fill in the table
        :param replace: Whether to replace instead of insert
        :return: The generated INSERT or REPLACE SQL statement
        """
        placeholders = [
            self.placeholder,
        ] * len(values)

        if target_fields:
            target_fields = ", ".join(target_fields)
            target_fields = f"({target_fields})"
        else:
            target_fields = ""

        if not replace:
            sql = "INSERT INTO "
        else:
            sql = "REPLACE INTO "
        sql += f"{table} {target_fields} VALUES ({','.join(placeholders)})"
        return sql

    def insert_rows(self, table, rows, target_fields=None, commit_every=1000, replace=False, **kwargs):
        """Insert a collection of tuples into a table.

        Rows are inserted in chunks, each chunk (of size ``commit_every``) is
        done in a new transaction.

        :param table: Name of the target table
        :param rows: The rows to insert into the table
        :param target_fields: The names of the columns to fill in the table
        :param commit_every: The maximum number of rows to insert in one
            transaction. Set to 0 to insert all rows in one transaction.
        :param replace: Whether to replace instead of insert
        """
        i = 0
        with closing(self.get_conn()) as conn:
            if self.supports_autocommit:
                self.set_autocommit(conn, False)

            conn.commit()

            with closing(conn.cursor()) as cur:
                for i, row in enumerate(rows, 1):
                    lst = []
                    for cell in row:
                        lst.append(self._serialize_cell(cell, conn))
                    values = tuple(lst)
                    sql = self._generate_insert_sql(table, values, target_fields, replace, **kwargs)
                    self.log.debug("Generated sql: %s", sql)
                    cur.execute(sql, values)
                    if commit_every and i % commit_every == 0:
                        conn.commit()
                        self.log.info("Loaded %s rows into %s so far", i, table)

            conn.commit()
        self.log.info("Done loading. Loaded a total of %s rows into %s", i, table)

    @staticmethod
    def _serialize_cell(cell, conn=None) -> str | None:
        """
        Return the SQL literal of the cell as a string.

        :param cell: The cell to insert into the table
        :param conn: The database connection
        :return: The serialized cell
        """
        if cell is None:
            return None
        if isinstance(cell, datetime):
            return cell.isoformat()
        return str(cell)

    def bulk_dump(self, table, tmp_file):
        """
        Dump a database table into a tab-delimited file.

        :param table: The name of the source table
        :param tmp_file: The path of the target file
        """
        raise NotImplementedError()

    def bulk_load(self, table, tmp_file):
        """
        Load a tab-delimited file into a database table.

        :param table: The name of the target table
        :param tmp_file: The path of the file to load into the table
        """
        raise NotImplementedError()

    def test_connection(self):
        """Tests the connection using db-specific query."""
        status, message = False, ""
        try:
            if self.get_first(self._test_connection_sql):
                status = True
                message = "Connection successfully tested"
        except Exception as e:
            status = False
            message = str(e)

        return status, message

    def get_openlineage_database_info(self, connection) -> DatabaseInfo | None:
        """
        Return database specific information needed to generate and parse lineage metadata.

        This includes information helpful for constructing information schema query
        and creating correct namespace.

        :param connection: Airflow connection to reduce calls of `get_connection` method
        """

    def get_openlineage_database_dialect(self, connection) -> str:
        """
        Return database dialect used for SQL parsing.

        For a list of supported dialects check: https://openlineage.io/docs/development/sql#sql-dialects
        """
        return "generic"

    def get_openlineage_default_schema(self) -> str | None:
        """
        Return default schema specific to database.

        .. seealso::
            - :class:`airflow.providers.openlineage.sqlparser.SQLParser`
        """
        return self.__schema or "public"

    def get_openlineage_database_specific_lineage(self, task_instance) -> OperatorLineage | None:
        """
        Return additional database specific lineage, e.g. query execution information.

        This method is called only on completion of the task.

        :param task_instance: this may be used to retrieve additional information
            that is collected during runtime of the task
        """

    @staticmethod
    def get_openlineage_authority_part(connection, default_port: int | None = None) -> str:
        """
        Get authority part from Airflow Connection.

        The authority represents the hostname and port of the connection
        and conforms OpenLineage naming convention for a number of databases (e.g. MySQL, Postgres, Trino).

        :param default_port: (optional) used if no port parsed from connection URI
        """
        parsed = urlparse(connection.get_uri())
        port = parsed.port or default_port
        if port:
            authority = f"{parsed.hostname}:{port}"
        else:
            authority = parsed.hostname
        return authority
