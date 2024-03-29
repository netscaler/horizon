from __future__ import division

from csv import DictWriter  # noqa
from csv import writer  # noqa

import datetime
import logging
from StringIO import StringIO  # noqa

from django.conf import settings  # noqa
from django.http import HttpResponse  # noqa
from django import template as django_template
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _  # noqa
from django import VERSION  # noqa

from horizon import exceptions
from horizon import forms
from horizon import messages

from openstack_dashboard import api
from openstack_dashboard.usage import quotas


LOG = logging.getLogger(__name__)


class BaseUsage(object):
    show_terminated = False

    def __init__(self, request, project_id=None):
        self.project_id = project_id or request.user.tenant_id
        self.request = request
        self.summary = {}
        self.usage_list = []
        self.limits = {}
        self.quotas = {}

    @property
    def today(self):
        return timezone.now()

    @staticmethod
    def get_start(year, month, day):
        start = datetime.datetime(year, month, day, 0, 0, 0)
        return timezone.make_aware(start, timezone.utc)

    @staticmethod
    def get_end(year, month, day):
        end = datetime.datetime(year, month, day, 23, 59, 59)
        return timezone.make_aware(end, timezone.utc)

    def get_instances(self):
        instance_list = []
        [instance_list.extend(u.server_usages) for u in self.usage_list]
        return instance_list

    def get_date_range(self):
        if not hasattr(self, "start") or not hasattr(self, "end"):
            args_start = args_end = (self.today.year,
                                     self.today.month,
                                     self.today.day)
            form = self.get_form()
            if form.is_valid():
                start = form.cleaned_data['start']
                end = form.cleaned_data['end']
                args_start = (start.year,
                              start.month,
                              start.day)
                args_end = (end.year,
                            end.month,
                            end.day)
            elif form.is_bound:
                messages.error(self.request,
                               _("Invalid date format: "
                                 "Using today as default."))
        self.start = self.get_start(*args_start)
        self.end = self.get_end(*args_end)
        return self.start, self.end

    def init_form(self):
        today = datetime.date.today()
        first = datetime.date(day=1, month=today.month, year=today.year)
        if today.day in range(5):
            self.end = first - datetime.timedelta(days=1)
            self.start = datetime.date(day=1,
                                       month=self.end.month,
                                       year=self.end.year)
        else:
            self.end = today
            self.start = first
        return self.start, self.end

    def get_form(self):
        if not hasattr(self, 'form'):
            if any(key in ['start', 'end'] for key in self.request.GET):
                # bound form
                self.form = forms.DateForm(self.request.GET)
            else:
                # non-bound form
                init = self.init_form()
                self.form = forms.DateForm(initial={'start': init[0],
                                                    'end': init[1]})
        return self.form

    def _get_neutron_usage(self, limits, resource_name):
        resource_map = {
            'floatingip': {
                'api': api.network.tenant_floating_ip_list,
                'limit_name': 'totalFloatingIpsUsed',
                'message': _('Unable to retrieve floating IP addresses.')
            },
            'security_group': {
                'api': api.network.security_group_list,
                'limit_name': 'totalSecurityGroupsUsed',
                'message': _('Unable to retrieve security gruops.')
            }
        }

        resource = resource_map[resource_name]
        try:
            method = resource['api']
            current_used = len(method(self.request))
        except Exception:
            current_used = 0
            msg = resource['message']
            exceptions.handle(self.request, msg)

        limits[resource['limit_name']] = current_used

    def _set_neutron_limit(self, limits, neutron_quotas, resource_name):
        limit_name_map = {
            'floatingip': 'maxTotalFloatingIps',
            'security_group': 'maxSecurityGroups',
        }
        if neutron_quotas is None:
            resource_max = float("inf")
        else:
            resource_max = getattr(neutron_quotas.get(resource_name),
                                   'limit', float("inf"))
            if resource_max == -1:
                resource_max = float("inf")

        limits[limit_name_map[resource_name]] = resource_max

    def get_neutron_limits(self):
        if not api.base.is_service_enabled(self.request, 'network'):
            return

        neutron_sg_used = \
            api.neutron.is_security_group_extension_supported(self.request)

        self._get_neutron_usage(self.limits, 'floatingip')
        if neutron_sg_used:
            self._get_neutron_usage(self.limits, 'security_group')

        # Quotas are an optional extension in Neutron. If it isn't
        # enabled, assume the floating IP limit is infinite.
        if api.neutron.is_quotas_extension_supported(self.request):
            try:
                neutron_quotas = api.neutron.tenant_quota_get(self.request,
                                                              self.project_id)
            except Exception:
                neutron_quotas = None
                msg = _('Unable to retrieve network quota information.')
                exceptions.handle(self.request, msg)
        else:
            neutron_quotas = None

        self._set_neutron_limit(self.limits, neutron_quotas, 'floatingip')
        if neutron_sg_used:
            self._set_neutron_limit(self.limits, neutron_quotas,
                                    'security_group')

    def get_limits(self):
        try:
            self.limits = api.nova.tenant_absolute_limits(self.request)
        except Exception:
            exceptions.handle(self.request,
                              _("Unable to retrieve limit information."))

        self.get_neutron_limits()

    def get_usage_list(self, start, end):
        raise NotImplementedError("You must define a get_usage_list method.")

    def summarize(self, start, end):
        if start <= end and start <= self.today:
            # The API can't handle timezone aware datetime, so convert back
            # to naive UTC just for this last step.
            start = timezone.make_naive(start, timezone.utc)
            end = timezone.make_naive(end, timezone.utc)
            try:
                self.usage_list = self.get_usage_list(start, end)
            except Exception:
                exceptions.handle(self.request,
                                  _('Unable to retrieve usage information.'))
        elif end < start:
            messages.error(self.request,
                           _("Invalid time period. The end date should be "
                             "more recent than the start date."))
        elif start > self.today:
            messages.error(self.request,
                           _("Invalid time period. You are requesting "
                             "data from the future which may not exist."))

        for project_usage in self.usage_list:
            project_summary = project_usage.get_summary()
            for key, value in project_summary.items():
                self.summary.setdefault(key, 0)
                self.summary[key] += value

    def get_quotas(self):
        try:
            self.quotas = quotas.tenant_quota_usages(self.request)
        except Exception:
            exceptions.handle(self.request,
                              _("Unable to retrieve quota information."))

    def csv_link(self):
        form = self.get_form()
        data = {}
        if hasattr(form, "cleaned_data"):
            data = form.cleaned_data
        if not ('start' in data and 'end' in data):
            data = {"start": self.today.date(), "end": self.today.date()}
        return "?start=%s&end=%s&format=csv" % (data['start'],
                                                data['end'])


class GlobalUsage(BaseUsage):
    show_terminated = True

    def get_usage_list(self, start, end):
        return api.nova.usage_list(self.request, start, end)


class ProjectUsage(BaseUsage):
    attrs = ('memory_mb', 'vcpus', 'uptime',
             'hours', 'local_gb')

    def get_usage_list(self, start, end):
        show_terminated = self.request.GET.get('show_terminated',
                                               self.show_terminated)
        instances = []
        terminated_instances = []
        usage = api.nova.usage_get(self.request, self.project_id, start, end)
        # Attribute may not exist if there are no instances
        if hasattr(usage, 'server_usages'):
            now = self.today
            for server_usage in usage.server_usages:
                # This is a way to phrase uptime in a way that is compatible
                # with the 'timesince' filter. (Use of local time intentional.)
                server_uptime = server_usage['uptime']
                total_uptime = now - datetime.timedelta(seconds=server_uptime)
                server_usage['uptime_at'] = total_uptime
                if server_usage['ended_at'] and not show_terminated:
                    terminated_instances.append(server_usage)
                else:
                    instances.append(server_usage)
        usage.server_usages = instances
        return (usage,)


class CsvDataMixin(object):

    """
    CSV data Mixin - provides handling for CSV data

    .. attribute:: columns

        A list of CSV column definitions. If omitted - no column titles
        will be shown in the result file. Optional.
    """
    def __init__(self):
        self.out = StringIO()
        super(CsvDataMixin, self).__init__()
        if hasattr(self, "columns"):
            self.writer = DictWriter(self.out, map(self.encode, self.columns))
            self.is_dict = True
        else:
            self.writer = writer(self.out)
            self.is_dict = False

    def write_csv_header(self):
        if self.is_dict:
            try:
                self.writer.writeheader()
            except AttributeError:
                # For Python<2.7
                self.writer.writerow(dict(zip(
                                          self.writer.fieldnames,
                                          self.writer.fieldnames)))

    def write_csv_row(self, args):
        if self.is_dict:
            self.writer.writerow(dict(zip(
                self.writer.fieldnames, map(self.encode, args))))
        else:
            self.writer.writerow(map(self.encode, args))

    def encode(self, value):
        # csv and StringIO cannot work with mixed encodings,
        # so encode all with utf-8
        return unicode(value).encode('utf-8')


class BaseCsvResponse(CsvDataMixin, HttpResponse):

    """
    Base CSV response class. Provides handling of CSV data.

    """

    def __init__(self, request, template, context, content_type, **kwargs):
        super(BaseCsvResponse, self).__init__()
        self['Content-Disposition'] = 'attachment; filename="%s"' % (
            kwargs.get("filename", "export.csv"),)
        self['Content-Type'] = content_type
        self.context = context
        self.header = None
        if template:
            # Display some header info if provided as a template
            header_template = django_template.loader.get_template(template)
            context = django_template.RequestContext(request, self.context)
            self.header = header_template.render(context)

        if self.header:
            self.out.write(self.encode(self.header))

        self.write_csv_header()

        for row in self.get_row_data():
            self.write_csv_row(row)

        self.out.flush()
        self.content = self.out.getvalue()
        self.out.close()

    def get_row_data(self):
        raise NotImplementedError("You must define a get_row_data method on %s"
                                  % self.__class__.__name__)

if VERSION >= (1, 5, 0):

    from django.http import StreamingHttpResponse  # noqa

    class BaseCsvStreamingResponse(CsvDataMixin, StreamingHttpResponse):

        """
        Base CSV Streaming class. Provides streaming response for CSV data.
        """

        def __init__(self, request, template, context, content_type, **kwargs):
            super(BaseCsvStreamingResponse, self).__init__()
            self['Content-Disposition'] = 'attachment; filename="%s"' % (
                kwargs.get("filename", "export.csv"),)
            self['Content-Type'] = content_type
            self.context = context
            self.header = None
            if template:
                # Display some header info if provided as a template
                header_template = django_template.loader.get_template(template)
                context = django_template.RequestContext(request, self.context)
                self.header = header_template.render(context)

            self._closable_objects.append(self.out)

            self.streaming_content = self.get_content()

        def buffer(self):
            buf = self.out.getvalue()
            self.out.truncate(0)
            return buf

        def get_content(self):
            if self.header:
                self.out.write(self.encode(self.header))

            self.write_csv_header()
            yield self.buffer()

            for row in self.get_row_data():
                self.write_csv_row(row)
                yield self.buffer()

        def get_row_data(self):
            raise NotImplementedError("You must define a get_row_data method "
                                      "on %s" % self.__class__.__name__)
