import django_filters
from actionlog.models import LogEntry

class LogEntryFilter(django_filters.FilterSet):
    class Meta:
        model = LogEntry
        fields = ['user', 'action_type']
