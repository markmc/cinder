# Copyright 2011 Justin Santa Barbara
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""The volumes api."""

from webob import exc
import webob

from cinder.api.openstack import common
from cinder.api.openstack import wsgi
from cinder.api.openstack import xmlutil
from cinder import exception
from cinder import flags
from cinder.openstack.common import log as logging
from cinder import volume
from cinder.volume import volume_types


LOG = logging.getLogger(__name__)


FLAGS = flags.FLAGS


def _translate_attachment_detail_view(_context, vol):
    """Maps keys for attachment details view."""

    d = _translate_attachment_summary_view(_context, vol)

    # No additional data / lookups at the moment

    return d


def _translate_attachment_summary_view(_context, vol):
    """Maps keys for attachment summary view."""
    d = {}

    volume_id = vol['id']

    # NOTE(justinsb): We use the volume id as the id of the attachment object
    d['id'] = volume_id

    d['volume_id'] = volume_id
    d['server_id'] = vol['instance_uuid']
    if vol.get('mountpoint'):
        d['device'] = vol['mountpoint']

    return d


def _translate_volume_detail_view(context, vol):
    """Maps keys for volumes details view."""

    d = _translate_volume_summary_view(context, vol)

    # No additional data / lookups at the moment

    return d


def _translate_volume_summary_view(context, vol):
    """Maps keys for volumes summary view."""
    d = {}

    d['id'] = vol['id']
    d['status'] = vol['status']
    d['size'] = vol['size']
    d['availability_zone'] = vol['availability_zone']
    d['created_at'] = vol['created_at']

    d['attachments'] = []
    if vol['attach_status'] == 'attached':
        attachment = _translate_attachment_detail_view(context, vol)
        d['attachments'].append(attachment)

    d['display_name'] = vol['display_name']
    d['display_description'] = vol['display_description']

    if vol['volume_type_id'] and vol.get('volume_type'):
        d['volume_type'] = vol['volume_type']['name']
    else:
        # TODO(bcwaldon): remove str cast once we use uuids
        d['volume_type'] = str(vol['volume_type_id'])

    d['snapshot_id'] = vol['snapshot_id']

    LOG.audit(_("vol=%s"), vol, context=context)

    if vol.get('volume_metadata'):
        meta_dict = {}
        for i in vol['volume_metadata']:
            meta_dict[i['key']] = i['value']
        d['metadata'] = meta_dict
    else:
        d['metadata'] = {}

    return d


def make_attachment(elem):
    elem.set('id')
    elem.set('server_id')
    elem.set('volume_id')
    elem.set('device')


def make_volume(elem):
    elem.set('id')
    elem.set('status')
    elem.set('size')
    elem.set('availability_zone')
    elem.set('created_at')
    elem.set('display_name')
    elem.set('display_description')
    elem.set('volume_type')
    elem.set('snapshot_id')

    attachments = xmlutil.SubTemplateElement(elem, 'attachments')
    attachment = xmlutil.SubTemplateElement(attachments, 'attachment',
                                            selector='attachments')
    make_attachment(attachment)

    metadata = xmlutil.make_flat_dict('metadata')
    elem.append(metadata)


volume_nsmap = {None: xmlutil.XMLNS_VOLUME_V1, 'atom': xmlutil.XMLNS_ATOM}


class VolumeTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('volume', selector='volume')
        make_volume(root)
        return xmlutil.MasterTemplate(root, 1, nsmap=volume_nsmap)


class VolumesTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('volumes')
        elem = xmlutil.SubTemplateElement(root, 'volume', selector='volumes')
        make_volume(elem)
        return xmlutil.MasterTemplate(root, 1, nsmap=volume_nsmap)


class VolumeController(object):
    """The Volumes API controller for the OpenStack API."""

    def __init__(self):
        self.volume_api = volume.API()
        super(VolumeController, self).__init__()

    @wsgi.serializers(xml=VolumeTemplate)
    def show(self, req, id):
        """Return data about the given volume."""
        context = req.environ['cinder.context']

        try:
            vol = self.volume_api.get(context, id)
        except exception.NotFound:
            raise exc.HTTPNotFound()

        return {'volume': _translate_volume_detail_view(context, vol)}

    def delete(self, req, id):
        """Delete a volume."""
        context = req.environ['cinder.context']

        LOG.audit(_("Delete volume with id: %s"), id, context=context)

        try:
            volume = self.volume_api.get(context, id)
            self.volume_api.delete(context, volume)
        except exception.NotFound:
            raise exc.HTTPNotFound()
        return webob.Response(status_int=202)

    @wsgi.serializers(xml=VolumesTemplate)
    def index(self, req):
        """Returns a summary list of volumes."""
        return self._items(req, entity_maker=_translate_volume_summary_view)

    @wsgi.serializers(xml=VolumesTemplate)
    def detail(self, req):
        """Returns a detailed list of volumes."""
        return self._items(req, entity_maker=_translate_volume_detail_view)

    def _items(self, req, entity_maker):
        """Returns a list of volumes, transformed through entity_maker."""
        context = req.environ['cinder.context']

        volumes = self.volume_api.get_all(context)
        limited_list = common.limited(volumes, req)
        res = [entity_maker(context, vol) for vol in limited_list]
        return {'volumes': res}

    @wsgi.serializers(xml=VolumeTemplate)
    def create(self, req, body):
        """Creates a new volume."""
        context = req.environ['cinder.context']

        if not body:
            raise exc.HTTPUnprocessableEntity()

        volume = body['volume']
        size = volume['size']
        LOG.audit(_("Create volume of %s GB"), size, context=context)

        kwargs = {}

        req_volume_type = volume.get('volume_type', None)
        if req_volume_type:
            try:
                kwargs['volume_type'] = volume_types.get_volume_type_by_name(
                        context, req_volume_type)
            except exception.NotFound:
                raise exc.HTTPNotFound()

        kwargs['metadata'] = volume.get('metadata', None)

        snapshot_id = volume.get('snapshot_id')
        if snapshot_id is not None:
            kwargs['snapshot'] = self.volume_api.get_snapshot(context,
                                                              snapshot_id)
        else:
            kwargs['snapshot'] = None

        kwargs['availability_zone'] = volume.get('availability_zone', None)

        new_volume = self.volume_api.create(context,
                                            size,
                                            volume.get('display_name'),
                                            volume.get('display_description'),
                                            **kwargs)

        # TODO(vish): Instance should be None at db layer instead of
        #             trying to lazy load, but for now we turn it into
        #             a dict to avoid an error.
        retval = _translate_volume_detail_view(context, dict(new_volume))

        return {'volume': retval}


def create_resource():
    return wsgi.Resource(VolumeController())
