"""This module implements the connection to the database."""
import json
from custom_logging import autosteer_logging
import numpy as np
import pandas as pd
import random
import socket
import sqlalchemy
from datetime import datetime
from sqlalchemy import create_engine, event
from sqlalchemy.sql import text
from sqlalchemy.exc import IntegrityError
import unittest

SCHEMA_FILE = 'schema.sql'
ENGINE = None
TESTED_DATABASE = None


def read_sql_file(filename, encoding='utf-8') -> str:
    """Read SQL file, remove comments, and return a list of sql statements as a string"""
    with open(filename, encoding=encoding) as f:
        file = f.read()
    statements = file.split('\n')
    return '\n'.join(filter(lambda line: not line.startswith('--'), statements))


# def load_extension(dbapi_conn, _):
#    conn.enable_load_extension(True)
#    conn.load_extension('./sqlean-extensions/stats.so')


def _db():
    global ENGINE
    url = f'sqlite:///results/{TESTED_DATABASE}.sqlite'
    autosteer_logging.debug('Connect to database: %s', url)
    ENGINE = create_engine(url)

    @event.listens_for(ENGINE, "connect")
    def connect(dbapi_conn, _):
        """Load SQLite extension for median calculation"""
        dbapi_conn.enable_load_extension(True)
        dbapi_conn.load_extension('./sqlean-extensions/stats.so')
        dbapi_conn.enable_load_extension(False)

    conn = ENGINE.connect()
    schema = read_sql_file(SCHEMA_FILE)

    for statement in schema.split(';'):
        if len(statement.strip()) > 0:
            try:
                conn.execute(statement)
            except Exception as e:
                print(e)
                raise e
    return conn


def register_query(query_path):
    with _db() as conn:
        try:
            stmt = text('INSERT INTO queries (query_path, result_fingerprint) VALUES (:query_path, :result_fingerprint )')
            conn.execute(stmt, query_path=query_path, result_fingerprint=None)
        except IntegrityError:
            pass


def register_query_fingerprint(query_path, fingerprint):
    with _db() as conn:
        result = conn.execute(text('SELECT result_fingerprint FROM queries WHERE query_path= :query_path'), query_path=query_path).fetchone()[0]
        if result is None:
            conn.execute(
                text('UPDATE queries SET result_fingerprint = :fingerprint WHERE query_path = :query_path;'), fingerprint=fingerprint, query_path=query_path)
            return True
        elif result != fingerprint:
            return False  # fingerprints do not match
        return True


def register_optimizer(query_path, optimizer, table):
    with _db() as conn:
        try:
            stmt = text(f'INSERT INTO {table} (query_id, optimizer) '
                        'SELECT id, :optimizer FROM queries WHERE query_path = :query_path')
            conn.execute(stmt, optimizer=optimizer, query_path=query_path)
        except IntegrityError:
            pass  # do not store duplicates


def register_optimizer_dependency(query_path, optimizer, dependency, table):
    with _db() as conn:
        try:
            stmt = text(f'INSERT INTO {table} (query_id, optimizer, dependent_optimizer) '
                        'SELECT id, :optimizer, :dependency FROM queries WHERE query_path = :query_path')
            conn.execute(stmt, optimizer=optimizer, dependency=dependency, query_path=query_path)
        except IntegrityError:
            pass  # do not store duplicates


class Measurement:
    """This class stores the measurement for a certain query and optimizer configuration"""

    def __init__(self, query_path, query_id, optimizer_config, disabled_rules,
                 num_disabled_rules, plan_json, running_time, cpu_time):
        self.query_path = query_path
        self.query_id = query_id
        self.optimizer_config = optimizer_config
        self.disabled_rules = disabled_rules
        self.num_disabled_rules = num_disabled_rules
        self.plan_json = json.loads(plan_json)
        self.running_time = running_time
        self.cpu_time = cpu_time


def experience(benchmark=None, training_ratio=0.8):
    """Get experience to train BAO"""
    stmt = f"""select qu.query_path, q.query_id, q.id,  q.disabled_rules, q.num_disabled_rules, q.logical_plan_json, elapsed, cpu_time
            from measurements m, query_optimizer_configs q, queries qu
            where m.query_optimizer_config_id = q.id
              and q.logical_plan_json != 'None' 
              and qu.id = q.query_id
              and qu.query_path like '%%{'' if benchmark is None else benchmark}%%'"""

    with _db() as conn:
        df = pd.read_sql(stmt, conn)
    default_median_runtimes = df.groupby(['query_path', 'query_id', 'id', 'disabled_rules', 'num_disabled_rules', 'logical_plan_json'])[
        'elapsed', 'cpu_time'].median().reset_index()
    rows = [Measurement(*row) for index, row in default_median_runtimes.iterrows()]

    # group training and test data by query
    result = {}
    for row in rows:
        if row.query_id in result:
            result[row.query_id].append(row)
        else:
            result[row.query_id] = [row]

    keys = list(result.keys())
    random.shuffle(keys)
    split_index = int(len(keys) * training_ratio)
    train_keys = keys[:split_index]
    test_keys = keys[split_index:]

    train_data = np.concatenate([result[key] for key in train_keys])
    test_data = np.concatenate([result[key] for key in test_keys])

    return train_data, test_data


def register_rule(query_path, rule, table):
    with _db() as conn:
        try:
            stmt = text(f'INSERT INTO {table} (query_id, rule) SELECT id, :rule FROM queries WHERE query_path = :query_path')
            conn.execute(stmt, rule=rule, query_path=query_path)
        except IntegrityError:
            pass  # do not store duplicates


def get_optimizers(table_name, query_path, projections):
    with _db() as conn:
        stmt = f"""
               SELECT {','.join(projections)}
               FROM queries q, {table_name} qro
               WHERE q.query_path='{query_path}' AND q.id = qro.query_id and optimizer != ''
               """
        cursor = conn.execute(stmt)
        return cursor.fetchall()


def get_required_optimizers(query_path):
    return list(map(lambda res: res[0], get_optimizers('query_required_optimizers', query_path, ['optimizer'])))


def get_effective_optimizers(query_path):
    return list(map(lambda res: res[0], get_optimizers('query_effective_optimizers', query_path, ['optimizer'])))


def get_effective_optimizers_depedencies(query_path):
    return list(map(lambda res: [res[0], res[1]], get_optimizers('query_effective_optimizers_dependencies', query_path, ['optimizer', 'dependent_optimizer'])))


def select_query(query):
    with _db() as conn:
        cursor = conn.execute(query)
        return [row[0] for row in cursor.fetchall()]


def get_df(query):
    with _db() as conn:
        df = pd.read_sql(query, conn)
        return df


def register_query_config(query_path, disabled_rules, query_plan, plan_hash):
    """
    Store the passed query optimizer configuration in the database.
    :returns: query plan is already known and a duplicate
    """
    check_for_duplicated_plans = """SELECT count(*)
        from queries q, query_optimizer_configs qoc
        where q.id = qoc.query_id
              and q.query_path = '{0}'
              and qoc.hash = {1}
              and qoc.disabled_rules != '{2}'"""
    result = select_query(check_for_duplicated_plans.format(query_path, plan_hash, disabled_rules))
    is_duplicate = result[0] > 0

    with _db() as conn:

        def literal_processor(val):
            return sqlalchemy.String('').literal_processor(dialect=ENGINE.dialect)(value=str(val))

        try:
            query_plan_processed = literal_processor(json.dumps(query_plan))

            num_disabled_rules = 0 if disabled_rules is None else disabled_rules.count(',') + 1
            stmt = f"""INSERT INTO query_optimizer_configs
                   (query_id, disabled_rules, query_plan, num_disabled_rules, hash, duplicated_plan) 
                   SELECT id, '{disabled_rules}', {query_plan_processed}, {num_disabled_rules}, {plan_hash}, {is_duplicate} from queries where query_path = '{query_path}'
                   """
            conn.execute(stmt)
        except IntegrityError:
            pass  # query configuration has already been inserted

    return is_duplicate


def check_for_existing_measurements(query_path, disabled_rules):
    query = f"""select count(*) as num_measurements
                from measurements m, query_optimizer_configs qoc, queries q
                where m.query_optimizer_config_id = qoc.id
                and qoc.query_id = q.id
                and q.query_path = '{query_path}'
                and qoc.disabled_rules = '{disabled_rules}'
             """
    df = get_df(query)
    values = df['num_measurements']
    return values[0] > 0


def register_measurement(query_path, disabled_rules, walltime, input_data_size, nodes):
    autosteer_logging.info('register a new measurement for query %s and the disabled rules/optimizers [%s]', query_path, disabled_rules)
    with _db() as conn:
        now = datetime.now()
        query = f"""INSERT INTO measurements (query_optimizer_config_id, walltime, machine, time, input_data_size, nodes)
                SELECT id, {walltime}, '{socket.gethostname()}', '{now.strftime('%m/%d/%Y, %H:%M:%S')}', {input_data_size}, {nodes} FROM query_optimizer_configs 
                WHERE query_id = (SELECT id from queries where query_path = '{query_path}') and disabled_rules = '{disabled_rules}'
                """
        conn.execute(query)


def median_runtimes():
    class OptimizerConfigResult:
        def __init__(self, path, num_disabled_rules, disabled_rules, json_plan, runtime):
            self.path = path
            self.num_disabled_rules = num_disabled_rules
            self.disabled_rules = disabled_rules
            self.json_plan = json_plan
            self.runtime = runtime

    with _db() as conn:
        default_plans_stmt = """
        select q.query_path, qoc.num_disabled_rules, qoc.disabled_rules, logical_plan_json, elapsed
        from queries q,
             query_optimizer_configs qoc,
             measurements m
        where q.id = qoc.query_id
          and qoc.id = m.query_optimizer_config_id
        """
        df = pd.read_sql(default_plans_stmt, conn)
        default_median_runtimes = df.groupby(['query_path', 'num_disabled_rules', 'disabled_rules', 'logical_plan_json'])['elapsed'].median().reset_index()

        return [OptimizerConfigResult(*row) for index, row in default_median_runtimes.iterrows()]


def best_alternative_configuration(benchmark=None, postgres=False):
    class OptimizerConfigResult:
        def __init__(self, path, num_disabled_rules, runtime, runtime_baseline, savings, disabled_rules, rank):
            self.path = path
            self.num_disabled_rules = num_disabled_rules
            self.runtime = runtime
            self.runtime_baseline = runtime_baseline
            self.savings = savings
            self.disabled_rules = disabled_rules
            self.rank = rank

    autosteer_logging.warning(f"Use {'postgres_stmt' if postgres else 'presto_stmt'} to get the best alternative configurations")

    postgres_stmt = f"""
with default_plans (query_path, running_time) as (
    select q.query_path, median(elapsed)
    from queries q,
         query_optimizer_configs qoc,
         measurements m
    where q.id = qoc.query_id
      and qoc.id = m.query_optimizer_config_id
      --and qoc.num_disabled_rules = 0
      and qoc.disabled_rules = 'None'
    group by q.query_path, qoc.num_disabled_rules, qoc.disabled_rules
    having median(elapsed) < 1000000000
)
        ,
     results(query_path, num_disabled_rules, runtime, runtime_baseline, savings, disabled_rules, rank) as (
         select q.query_path,
                qoc.num_disabled_rules,
                median(m.elapsed),
                dp.running_time,
                (dp.running_time - median(m.elapsed)) / dp.running_time                     as savings,
                qoc.disabled_rules,
                dense_rank() over (
                    partition by q.query_path
                    order by (dp.running_time - median(m.elapsed)) / dp.running_time desc ) as ranki
         from queries q,
              query_optimizer_configs qoc,
              measurements m,
              default_plans dp
         where q.id = qoc.query_id
           and qoc.id = m.query_optimizer_config_id
           and dp.query_path = q.query_path
           and qoc.num_disabled_rules > 0
         group by q.query_path, qoc.num_disabled_rules, qoc.disabled_rules, dp.running_time
         order by savings desc)
select *
from results
where rank = 1
and query_path like '%%{'' if benchmark is None else benchmark}%%'
order by savings desc;
"""
    # this stmt use for presto
    stmt = f"""
       with default_plans (query_path, running_time) as (
        select q.query_path, median(m.running + m.finishing)
        from queries q,
             query_optimizer_configs qoc,
             measurements m
        where q.id = qoc.query_id
          and qoc.id = m.query_optimizer_config_id
          and qoc.num_disabled_rules = 0
          and qoc.disabled_rules = 'None'
        group by q.query_path, qoc.num_disabled_rules, qoc.disabled_rules),
         results(query_path, num_disabled_rules, runtime, runtime_baseline, savings, disabled_rules, rank) as (
             select q.query_path,
                    qoc.num_disabled_rules,
                    median(m.running + m.finishing),
                    dp.running_time,
                    (dp.running_time - median(m.running + m.finishing)) / dp.running_time as savings,
                    qoc.disabled_rules,
                    dense_rank() over (
                        partition by q.query_path
                        order by (dp.running_time - median(m.running + m.finishing)) / dp.running_time desc ) as ranki
             from queries q,
                  query_optimizer_configs qoc,
                  measurements m,
                  default_plans dp
             where q.id = qoc.query_id
               and qoc.id = m.query_optimizer_config_id
               and dp.query_path = q.query_path
               and qoc.num_disabled_rules > 0
             group by q.query_path, qoc.num_disabled_rules, qoc.disabled_rules, dp.running_time
             order by savings desc)
    select *
    from results
    where rank = 1
    and query_path like '%%{'' if benchmark is None else benchmark}%%'
    order by savings desc;"""

    with _db() as conn:
        cursor = conn.execute(postgres_stmt if postgres else stmt, )
        return [OptimizerConfigResult(*row) for row in cursor.fetchall()]


class TestStorage(unittest.TestCase):
    """Test the storage for benchmarks"""

    def test_median(self):
        with _db() as db:
            result = db.execute('SELECT MEDIAN(a) FROM (SELECT 1 AS a) AS tab').fetchall()
            assert len(result) == 1

    def test_queries(self):
        with _db() as db:
            result = db.execute('SELECT * from queries').fetchall()
            print(result)

    def test_optimizers(self):
        with _db() as db:
            result = db.execute('SELECT * from query_effective_optimizers;')
            print(result.fetchall())
