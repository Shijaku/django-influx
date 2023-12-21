from functools import lru_cache
from datetime import timedelta
from .criteria import Criteria, DisjunctionCriteria, WhereOperatorEnum, InjunctionCriteria, MathCriteria, Field
from .function import aggregations
from ..response import InfluxDBResponse
from ..serializers import BaseSerializer
from ..helpers.utils import generate_interval_string, generate_seconds
from .. import exceptions
from ..app import Influxable

FILTERS = {
    'gt': WhereOperatorEnum.GT,
    'gte': WhereOperatorEnum.GTE,
    'lt': WhereOperatorEnum.LT,
    'lte': WhereOperatorEnum.LTE,
    'eq': WhereOperatorEnum.EQ,
    'ne': WhereOperatorEnum.NE,
    'in': WhereOperatorEnum.EQ,
    'regex': WhereOperatorEnum.RE,
    'iregex': WhereOperatorEnum.RE,
    'startswith': WhereOperatorEnum.RE,
    'istartswith': WhereOperatorEnum.RE,
    'endswith': WhereOperatorEnum.RE,
    'iendswith': WhereOperatorEnum.RE,
    'range': None,
}


def process_filter(key, value):
    if '__' in key:
        key, lookup = key.split('__')
        if lookup not in FILTERS:
            raise exceptions.InfluxDBAttributeValueError('Invalid lookup: {}'.format(lookup))
        if lookup == 'in':
            return DisjunctionCriteria(*[Criteria(key, item, WhereOperatorEnum.EQ) for item in list(value)])
        elif lookup in ['regex', 'iregex', 'startswith', 'istartswith', 'endswith', 'iendswith']:
            return Criteria(key, f"\\{value}\\", WhereOperatorEnum.RE)
        elif lookup == 'range':
            return InjunctionCriteria(
                Criteria(key, value[0], WhereOperatorEnum.GTE),
                Criteria(key, value[1], WhereOperatorEnum.LTE)
            )
        else:
            return Criteria(key, value, FILTERS[lookup])
    else:
        return Criteria(key, value, WhereOperatorEnum.EQ)


class Q:
    def __init__(self, *args, **kwargs):
        self.all_criteria = list(args) or []
        for key, value in kwargs.items():
            self.all_criteria.append(process_filter(key, value))

    def __or__(self, other):
        return DisjunctionCriteria(self, other)

    def evaluate(self):
        if len(self.all_criteria) == 1:
            return self.all_criteria[0].evaluate()
        else:
            return '({})'.format(
                ' AND '.join([c.evaluate() for c in self.all_criteria])
            )


class Now:

    @staticmethod
    def get_duration(duration):
        if isinstance(duration, timedelta):
            return duration
        if isinstance(duration, str):
            return timedelta(seconds=generate_seconds(duration))
        if isinstance(duration, int):
            return timedelta(seconds=duration)

    def __add__(self, other):
        return Criteria('now()', self.get_duration(other), WhereOperatorEnum.ADD)

    def __sub__(self, other):
        return Criteria('now()', self.get_duration(other), WhereOperatorEnum.SUB)

    def evaluate(self):
        return 'now()'


class AS:
    def __init__(self, annotation_filter, annotation=None):
        if isinstance(annotation_filter, str):
            self.filter = annotation_filter
        else:
            self.filter = annotation_filter.evaluate()
        self.annotation = annotation

    def evaluate(self):
        if self.annotation:
            return f'{self.filter} AS {self.annotation}'
        return self.filter


class RawQuery:
    def __init__(self, str_query=''):
        # self.db = db
        self.str_query = str_query

    def execute(self):
        return self.raw_response

    @property
    def query(self):
        return self.str_query

    @property
    def raw_response(self):
        return self._resolve(db=getattr(self, "db", None))

    @lru_cache(maxsize=None)
    def _resolve(self, *args, **kwargs):
        instance = Influxable.get_instance()
        return instance.execute_query(query=self.str_query, method='post', database=kwargs.get('db'))


class SelectQueryClause:

    def __init__(self):
        super(SelectQueryClause, self).__init__()
        self.select_clause = 'SELECT {fields}'
        self.selected_fields = []
        self.default_selected_fields = '*'

    @staticmethod
    def validate_field(field):
        if not isinstance(field, str):
            msg = 'field type must be <str>'
            raise exceptions.InfluxDBInvalidTypeError(msg)

    def select(self, *fields):
        selected_fields = self.selected_fields
        for field in fields:
            if not hasattr(field, 'evaluate'):
                self.validate_field(field)
                evaluated_field = str(field)
            else:
                evaluated_field = field.evaluate()
            selected_fields.append(evaluated_field)
        self.selected_fields = selected_fields
        return self

    def only(self, *fields):
        return self.select(*fields)

    def _prepare_select_clause(self):
        if len(self.selected_fields):
            joined_fields = ','.join(self.selected_fields)
        else:
            joined_fields = self.default_selected_fields
        return self.select_clause.format(fields=joined_fields)


class IntoQueryClause:
    def __init__(self):
        super(IntoQueryClause, self).__init__()
        self.into_clause = 'INTO {measurement}'
        self.selected_into_measurement = None

    def validate_measurement(self, measurement):
        if not isinstance(measurement, str):
            msg = 'measurement type must be <str>'
            raise exceptions.InfluxDBInvalidTypeError(msg)

    def into(self, measurement):
        self.validate_measurement(measurement)
        self.selected_into_measurement = measurement
        return self

    def _prepare_into_clause(self):
        into_clause = ''
        if self.selected_into_measurement is not None:
            into_clause = self.into_clause.format(
                measurement=self.selected_into_measurement,
            )
        return into_clause


class FromQueryClause:
    def __init__(self):
        super(FromQueryClause, self).__init__()
        self.from_clause = 'FROM {measurements}'
        self.selected_measurements = 'default'

    def validate_measurements(self, measurements):
        if len(measurements) == 0:
            msg = 'measurements should not be empty'
            raise exceptions.InfluxDBInvalidTypeError(msg)
        for measurement in measurements:
            if not isinstance(measurement, str):
                msg = 'measurement type must be <str>'
                raise exceptions.InfluxDBInvalidTypeError(msg)

    def from_measurements(self, *measurements):
        self.validate_measurements(measurements)
        quoted_measurements = ['"{}"'.format(m) for m in measurements]
        self.selected_measurements = ','.join(quoted_measurements)
        return self

    def _prepare_from_clause(self):
        return self.from_clause.format(measurements=self.selected_measurements)

    def subquery(self, subquery):
        if not isinstance(subquery, Query):
            msg = 'subquery must be a Query instance'
            raise exceptions.InfluxDBInvalidTypeError(msg)
        self.selected_measurements = f'({subquery._get_prepared_query()})'
        return self


class WhereQueryClause:
    def __init__(self):
        super(WhereQueryClause, self).__init__()
        self.where_clause = 'WHERE {criteria}'
        self.selected_criteria = []

    def validate_criteria(self, criteria):
        if len(criteria) == 0:
            msg = 'criteria should not be empty'
            raise exceptions.InfluxDBInvalidTypeError(msg)
        for c in criteria:
            if not isinstance(c, (Criteria, DisjunctionCriteria, InjunctionCriteria, MathCriteria, AS, Q)):
                msg = 'Invalid criteria'
                raise exceptions.InfluxDBInvalidTypeError(msg)

    def where(self, *criteria):
        self.validate_criteria(criteria)
        self.selected_criteria = list(criteria)
        return self

    def filter(self, *args, **kwargs):
        all_criteria = list(args) or []
        for key, value in kwargs.items():
            all_criteria.append(process_filter(key, value))
        self.validate_criteria(all_criteria)
        self.selected_criteria.extend(all_criteria)
        return self

    def exclude(self, *args, **kwargs):
        all_criteria = args or []
        for key, value in kwargs.items():
            all_criteria.append(~process_filter(key, value))
        self.validate_criteria(all_criteria)
        self.selected_criteria.extend(all_criteria)

    def _prepare_where_clause(self):
        where_clause = ''
        if len(self.selected_criteria):
            criteria = [c.evaluate() for c in self.selected_criteria]
            eval_criteria = ' AND '.join(criteria)
            where_clause = self.where_clause.format(criteria=eval_criteria)
        return where_clause


class LimitQueryClause:
    def __init__(self):
        super(LimitQueryClause, self).__init__()
        self.limit_value = None

    def validate_value(self, value):
        if type(value) != int or value <= 0:
            msg = 'value must be a positive integer'
            raise exceptions.InfluxDBInvalidTypeError(msg)

    def limit(self, value):
        self.validate_value(value)
        self.limit_value = value
        return self

    def _prepare_limit_clause(self):
        limit_clause = ''
        if self.limit_value is not None:
            limit_clause = 'LIMIT {}'.format(self.limit_value)
        return limit_clause


class SLimitQueryClause:
    def __init__(self):
        super(SLimitQueryClause, self).__init__()
        self.slimit_value = None

    def validate_value(self, value):
        if type(value) != int or value <= 0:
            msg = 'value must be a positive integer'
            raise exceptions.InfluxDBInvalidTypeError(msg)

    def slimit(self, value):
        self.validate_value(value)
        self.slimit_value = value
        return self

    def _prepare_slimit_clause(self):
        slimit_clause = ''
        if self.slimit_value is not None:
            slimit_clause = 'SLIMIT {}'.format(self.slimit_value)
        return slimit_clause


class OffsetQueryClause:
    def __init__(self):
        super(OffsetQueryClause, self).__init__()
        self.offset_value = None

    def validate_value(self, value):
        if type(value) != int or value <= 0:
            msg = 'value must be a positive integer'
            raise exceptions.InfluxDBInvalidTypeError(msg)

    def offset(self, value):
        self.validate_value(value)
        self.offset_value = value
        return self

    def _prepare_offset_clause(self):
        offset_clause = ''
        if self.offset_value is not None:
            offset_clause = 'OFFSET {}'.format(self.offset_value)
        return offset_clause


class SOffsetQueryClause:
    def __init__(self):
        super(SOffsetQueryClause, self).__init__()
        self.soffset_value = None

    def validate_value(self, value):
        if type(value) != int or value <= 0:
            msg = 'value must be a positive integer'
            raise exceptions.InfluxDBInvalidTypeError(msg)

    def soffset(self, value):
        self.validate_value(value)
        self.soffset_value = value
        return self

    def _prepare_soffset_clause(self):
        soffset_clause = ''
        if self.soffset_value is not None:
            soffset_clause = 'SOFFSET {}'.format(self.soffset_value)
        return soffset_clause


class GroupByQueryClause:
    def __init__(self):
        super(GroupByQueryClause, self).__init__()
        self.group_by_clause = 'GROUP BY {}'
        self.time_subclause = 'time({params})'
        self.fill_subclause = 'fill({fill})'
        self.selected_group_by_tags = ['*']
        self.has_group_by_tags = False
        self.has_group_by_time = False
        self.interval_value = None
        self.shift_value = None
        self.fill_value = None

    @staticmethod
    def validate_tags(tags):
        if len(tags) == 0:
            msg = 'tags should not be empty'
            raise exceptions.InfluxDBInvalidTypeError(msg)
        for tag in tags:
            if not isinstance(tag, str):
                msg = 'tag type must be <str>'
                raise exceptions.InfluxDBInvalidTypeError(msg)

    @staticmethod
    def validate_fill(value):
        if type(value) is not int:
            msg = 'fill type must be numeric'
            raise exceptions.InfluxDBInvalidTypeError(msg)

    def validate_range_by(self, interval, shift, fill, tags):
        if not isinstance(tags, list) and not isinstance(tags, tuple):
            msg = 'tags must be a list'
            raise exceptions.InfluxDBInvalidTypeError(msg)
        if len(tags):
            self.validate_tags(tags)
        if fill is not None:
            self.validate_fill(fill)

    def group_by(self, *tags, shift=None, fill=None):
        if len(tags):
            interval = next((tag for tag in tags if isinstance(tag, timedelta)), None)
            reg_tags = [tag for tag in tags if not isinstance(tag, timedelta)]

            if interval:
                if reg_tags:
                    self.validate_tags(reg_tags)
                return self.range_by(interval, shift=shift, fill=fill, tags=reg_tags)
            self.validate_tags(reg_tags)
            self.selected_group_by_tags = list(tags)
        self.has_group_by_time = False
        self.has_group_by_tags = True
        return self

    def range_by(self, interval, shift=None, fill=None, tags=[]):
        if isinstance(interval, timedelta):
            interval = generate_interval_string(interval)
        self.validate_range_by(interval, shift, fill, tags)
        if len(tags):
            self.selected_group_by_tags = tags
        else:
            self.selected_group_by_tags = []
        self.has_group_by_tags = False
        self.has_group_by_time = True
        self.interval_value = interval
        self.shift_value = shift
        self.fill_value = fill
        return self

    def _prepare_time_subclause(self):
        if self.shift_value:
            params = ','.join([self.interval_value, self.shift_value])
        else:
            params = self.interval_value
        time_subclause = self.time_subclause.format(params=params)
        return time_subclause

    def _prepare_fill_subclause(self):
        fill_subclause = ''
        if self.fill_value:
            fill_subclause = self.fill_subclause.format(fill=self.fill_value)
        return fill_subclause

    def _prepare_group_by_tags_clause(self):
        tags = ', '.join(self.selected_group_by_tags)
        group_by_clause = self.group_by_clause.format(tags)
        return group_by_clause

    def _prepare_group_by_time_clause(self):
        tags = ','.join(self.selected_group_by_tags)
        time_subclause = self._prepare_time_subclause()
        fill_subclause = self._prepare_fill_subclause()
        temp_clause = time_subclause
        if tags:
            temp_clause += ','
            temp_clause += tags
        if fill_subclause:
            temp_clause += ' '
            temp_clause += fill_subclause
        group_by_clause = self.group_by_clause.format(temp_clause)
        return group_by_clause

    def _prepare_group_by_clause(self):
        group_by_clause = ''
        if self.has_group_by_tags:
            group_by_clause = self._prepare_group_by_tags_clause()
        if self.has_group_by_time:
            group_by_clause = self._prepare_group_by_time_clause()
        return group_by_clause


class OrderByQueryClause:
    def __init__(self):
        super(OrderByQueryClause, self).__init__()
        self.is_chronological_sort = None
        self.field = None

    def asc(self):
        self.is_chronological_sort = True
        return self

    def desc(self):
        self.is_chronological_sort = False
        return self

    def order_by(self, field):
        self.is_chronological_sort = True
        self.field = field
        if field.startswith('-'):
            self.is_chronological_sort = False
            self.field = field[1:]
        return self

    def _prepare_order_by_clause(self):
        order_by_clause = ''
        if self.is_chronological_sort is not None:
            order_by_value = 'ASC' if self.is_chronological_sort else 'DESC'
            order_by_clause = 'ORDER BY {}'.format(order_by_value)
            if self.field is not None:
                order_by_clause = 'ORDER BY {} {}'.format(self.field, order_by_value)
        return order_by_clause


class TimezoneClause:
    def __init__(self):
        super(TimezoneClause, self).__init__()
        self.timezone_clause = 'tz(\'{timezone}\')'
        self.timezone_value = None

    def validate_timezone(self, value):
        import pytz
        if not isinstance(value, str):
            msg = 'value must be a timezone string'
            raise exceptions.InfluxDBInvalidTypeError(msg)

        if value not in pytz.all_timezones:
            msg = 'value is an invalid timezone'
            raise exceptions.InfluxDBInvalidTypeError(msg)

    def tz(self, value):
        self.validate_timezone(value)
        self.timezone_value = value
        return self

    def _prepare_timezone_clause(self):
        timezone_value = ''
        if self.timezone_value is not None:
            timezone_value = self.timezone_clause.format(
                timezone=self.timezone_value,
            )
        return timezone_value


class SelectAggregation:

    @staticmethod
    def generate_values(values):
        if not values:
            return '*'
        if len(values) == 1:
            return f"/^{values[0]}$/"
        else:
            regex_value = '|'.join(values)
            return f"/{regex_value}/"

    def count(self, *values):
        return self.select(aggregations.Count(self.generate_values(values)))

    def distinct(self, value="*"):
        return self.select(aggregations.Distinct(value))

    def integral(self, *values):
        return self.select(aggregations.Integral(self.generate_values(values)))

    def mean(self, value='*'):
        return self.select(aggregations.Mean(value))

    def median(self, value='*'):
        return self.select(aggregations.Median(value))

    def mode(self, value='*'):
        return self.select(aggregations.Mode(value))

    def spread(self, value='*'):
        return self.select(aggregations.Spread(value))

    def std_dev(self, value='*'):
        return self.select(aggregations.StdDev(value))

    def sum(self, value='*'):
        return self.select(aggregations.Sum(value))

    def annotate(self, **kwargs):
        all_annotations = []
        for key, value in kwargs.items():
            all_annotations.append(AS(value, annotation=key))
        return self.select(*all_annotations)


class GenericQuery(
    SelectQueryClause,
    IntoQueryClause,
    FromQueryClause,
    WhereQueryClause,
    LimitQueryClause,
    SLimitQueryClause,
    OffsetQueryClause,
    SOffsetQueryClause,
    GroupByQueryClause,
    OrderByQueryClause,
    TimezoneClause,
    SelectAggregation,
):
    def __init__(self, *args, **kwargs):
        super(GenericQuery, self).__init__()


class Query(GenericQuery, RawQuery):
    def __init__(self, db=None):
        self.db = db
        super(Query, self).__init__()

    def _get_initial_query(self):
        initial_query = ' '.join([
            '{select_clause}',
            '{into_clause}',
            '{from_clause}',
            '{where_clause}',
            '{offset_clause}',
            '{slimit_clause}',
            '{soffset_clause}',
            '{group_by_clause}',
            '{order_by_clause}',
            '{timezone_clause}',
            '{limit_clause}',
        ])
        return initial_query

    def explain(self):
        return self._get_prepared_query()

    def _get_prepared_query(self):
        initial_query = self._get_initial_query()
        select_clause = self._prepare_select_clause()
        into_clause = self._prepare_into_clause()
        from_clause = self._prepare_from_clause()
        where_clause = self._prepare_where_clause()
        limit_clause = self._prepare_limit_clause()
        offset_clause = self._prepare_offset_clause()
        slimit_clause = self._prepare_slimit_clause()
        soffset_clause = self._prepare_soffset_clause()
        group_by_clause = self._prepare_group_by_clause()
        order_by_clause = self._prepare_order_by_clause()
        timezone_clause = self._prepare_timezone_clause()
        prepared_query = initial_query.format(
            select_clause=select_clause,
            into_clause=into_clause,
            from_clause=from_clause,
            where_clause=where_clause,
            limit_clause=limit_clause,
            offset_clause=offset_clause,
            slimit_clause=slimit_clause,
            soffset_clause=soffset_clause,
            group_by_clause=group_by_clause,
            order_by_clause=order_by_clause,
            timezone_clause=timezone_clause,
        )
        prepared_query = ' '.join(prepared_query.split())
        prepared_query = prepared_query.strip()
        return prepared_query

    def execute(self):
        prepared_query = self._get_prepared_query()
        # print('prepared_query', prepared_query)
        self.str_query = prepared_query
        return super().execute()

    def format(self, result, parser_class=BaseSerializer, **kwargs):
        return parser_class(result, **kwargs).convert()

    def evaluate(self, parser_class=BaseSerializer, **kwargs):
        result = InfluxDBResponse(self.execute())
        result.raise_if_error()
        formatted_result = self.format(result, parser_class, **kwargs)
        return formatted_result


class BulkInsertQuery(RawQuery):

    def __init__(self, str_points, db=None):
        super(BulkInsertQuery, self).__init__(str_points)
        self.db = db

    @lru_cache(maxsize=None)
    def _resolve(self, *args, **kwargs):
        instance = Influxable.get_instance()
        return instance.write_points(points=self.str_query, database=kwargs.get('db'))
