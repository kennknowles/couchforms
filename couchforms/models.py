from __future__ import absolute_import

import datetime
from couchdbkit.ext.django.schema import *
import couchforms.const as const
from dimagi.utils.indicators import ComputedDocumentMixin
from dimagi.utils.parsing import string_to_datetime
from dimagi.utils.couch.safe_index import safe_index
from dimagi.utils.couch.database import get_safe_read_kwargs, SafeSaveDocument
from xml.etree import ElementTree
from django.utils.datastructures import SortedDict
from couchdbkit.resource import ResourceNotFound
import logging
import hashlib
from copy import copy
from couchforms.signals import xform_archived
from dimagi.utils.mixins import UnicodeMixIn
from couchforms.const import ATTACHMENT_NAME
from couchdbkit.exceptions import PreconditionFailed
import time

def doc_types():
    """
    Mapping of doc_type attributes in CouchDB to the class that should be instantiated.
    """
    return {
        'XFormInstance': XFormInstance,
        'XFormArchived': XFormArchived,
        'XFormDeprecated': XFormDeprecated,
        'XFormDuplicate': XFormDuplicate,
        'XFormError': XFormError,
    }


def get(doc_id):
    # This logic is independent of couchforms; when it moves elsewhere, 
    # please use the most appropriate alternative to get a DB handle.
    
    db = XFormInstance.get_db()
    doc = db.get(doc_id)
    if doc['doc_type'] in doc_types():
        return doc_types()[doc['doc_type']].wrap(doc)
    raise ResourceNotFound(doc_id)


class Metadata(DocumentSchema):
    """
    Metadata of an xform, from a meta block structured like:
        
        <Meta>
            <timeStart />
            <timeEnd />
            <instanceID />
            <userID />
            <deviceID />
            <deprecatedID /> 
            <username />
        </Meta>
    
    See spec: https://bitbucket.org/javarosa/javarosa/wiki/OpenRosaMetaDataSchema
    
    username is not part of the spec but included for convenience
    """
    timeStart = DateTimeProperty()
    timeEnd = DateTimeProperty()
    instanceID = StringProperty()
    userID = StringProperty()
    deviceID = StringProperty()
    deprecatedID = StringProperty()
    username = StringProperty()

class XFormInstance(SafeSaveDocument, UnicodeMixIn, ComputedDocumentMixin):
    """An XForms instance."""
    xmlns = StringProperty()
    received_on = DateTimeProperty()
    partial_submission = BooleanProperty(default=False) # Used to tag forms that were forcefully submitted without a touchforms session completing normally
    
    @property
    def get_form(self):
        """public getter for the xform's form instance, it's redundant with 
        _form but wrapping that access gives future audit capabilities"""
        return self._form
    

    @classmethod
    def get(cls, docid, rev=None, db=None, dynamic_properties=True):
        # copied and tweaked from the superclass's method
        if not db:
            db = cls.get_db()
        cls._allow_dynamic_properties = dynamic_properties
        # on cloudant don't get the doc back until all nodes agree
        # on the copy, to avoid race conditions
        extras = get_safe_read_kwargs()
        return db.get(docid, rev=rev, wrapper=cls.wrap, **extras)

    @property
    def _form(self):
        return self[const.TAG_FORM]
    
    @property
    def type(self):
        return self._form.get(const.TAG_TYPE, "")
        
    @property
    def name(self):
        return self._form.get(const.TAG_NAME, "")

    @property
    def version(self):
        return self._form.get(const.TAG_VERSION, "")
        
    @property
    def uiversion(self):
        return self._form.get(const.TAG_UIVERSION, "")
    
    @property
    def metadata(self):
        if (const.TAG_META) in self._form:
            def _clean(meta_block):
                # couchdbkit chokes on dates that aren't actually dates
                # so check their validity before passing them up
                ret = copy(dict(meta_block))
                if meta_block:
                    for key in ("timeStart", "timeEnd"):
                        if key in meta_block:
                            if meta_block[key]:
                                try:
                                    parsed = string_to_datetime(meta_block[key])
                                    ret[key] = parsed
                                except ValueError:
                                    # we couldn't parse it
                                    del ret[key]
                            else:
                                # it was empty, also a failure
                                del ret[key]
                    # also clean dicts, since those are not allowed
                    for key in meta_block:
                        if isinstance(meta_block[key], dict):
                            ret[key] = ", ".join(\
                                "%s:%s" % (k, v) \
                                for k, v in meta_block[key].items())
                return ret
            return Metadata(_clean(self._form[const.TAG_META]))
        
        return None

    def __unicode__(self):
        return "%s (%s)" % (self.type, self.xmlns)

    def save(self, **kwargs):
        # HACK: cloudant has a race condition when saving newly created forms
        # which throws errors here. use a try/retry loop here to get around
        # it until we find something more stable.
        RETRIES = 10
        SLEEP = 0.5 # seconds
        tries = 0
        while True:
            try:
                return super(XFormInstance, self).save(**kwargs)
            except PreconditionFailed:
                if tries == 0:
                    logging.error('doc %s got a precondition failed' % self._id)
                if tries < RETRIES:
                    tries += 1
                    time.sleep(SLEEP)
                else:
                    raise

    def xpath(self, path):
        """
        Evaluates an xpath expression like: path/to/node and returns the value 
        of that element, or None if there is no value.
        """
        return safe_index(self, path.split("/"))
    
        
    def found_in_multiselect_node(self, xpath, option):
        """
        Whether a particular value was found in a multiselect node, referenced
        by path.
        """
        node = self.xpath(xpath)
        return node and option in node.split(" ")
    
    def get_xml(self):
        try:
            return self.fetch_attachment(ATTACHMENT_NAME)
        except ResourceNotFound:
            logging.warn("no xml found for %s, trying old attachment scheme." % self.get_id)
            return self[const.TAG_XML]
    
    @property
    def attachments(self):
        """
        Get the extra attachments for this form. This will not include
        the form itself
        """
        return dict((item, val) for item, val in self._attachments.items() if item != ATTACHMENT_NAME)
    
    def xml_md5(self):
        return hashlib.md5(self.get_xml().encode('utf-8')).hexdigest()
    
    def top_level_tags(self):
        """
        Returns a SortedDict of the top level tags found in the xml, in the
        order they are found.
        
        """
        xml_payload = self.get_xml()
        element = ElementTree.XML(xml_payload)
        to_return = SortedDict()
        for child in element:
            # fix {namespace}tag format forced by ElementTree in certain cases (eg, <reg> instead of <n0:reg>)
            key = child.tag.split('}')[1] if child.tag.startswith("{") else child.tag 
            if key == "Meta":
                key = "meta"
            to_return[key] = self.xpath('form/' + key)
        return to_return

    def archive(self):
        self.doc_type = "XFormArchived"
        self.save()
        xform_archived.send(sender="couchforms", xform=self)

class XFormError(XFormInstance):
    """
    Instances that have errors go here.
    """
    problem = StringProperty()
    
    def save(self, *args, **kwargs):
        # we put this here, in case the doc hasn't been modified from an original 
        # XFormInstance we'll force the doc_type to change. 
        self["doc_type"] = "XFormError" 
        super(XFormError, self).save(*args, **kwargs)
        
class XFormDuplicate(XFormError):
    """
    Duplicates of instances go here.
    """
    
    def save(self, *args, **kwargs):
        # we put this here, in case the doc hasn't been modified from an original 
        # XFormInstance we'll force the doc_type to change. 
        self["doc_type"] = "XFormDuplicate" 
        # we can't use super because XFormError also sets the doc type
        XFormInstance.save(self, *args, **kwargs)

class XFormDeprecated(XFormError):
    """
    After an edit, the old versions go here.
    """
    deprecated_date = DateTimeProperty(default=datetime.datetime.utcnow)
    
    def save(self, *args, **kwargs):
        # we put this here, in case the doc hasn't been modified from an original 
        # XFormInstance we'll force the doc_type to change. 
        self["doc_type"] = "XFormDeprecated" 
        # we can't use super because XFormError also sets the doc type
        XFormInstance.save(self, *args, **kwargs)
        # should raise a signal saying that this thing got deprecated

class XFormArchived(XFormError):
    """
    Archived forms don't show up in reports
    """
    archived_date = DateTimeProperty(default=datetime.datetime.utcnow)

    def save(self, *args, **kwargs):
        # force set the doc type and call the right superclass
        self["doc_type"] = "XFormArchived"
        XFormInstance.save(self, *args, **kwargs)

class SubmissionErrorLog(XFormError):
    """
    When a hard submission error (typically bad XML) is received we save it 
    here. 
    """
    md5 = StringProperty()
    
    # this is here as a bit of an annoying hack so that __unicode__ works
    # when called from the base class
    form = DictProperty()
        
    def __unicode__(self):
        return "Doc id: %s, Error %s" % (self.get_id, self.problem) 

    def get_xml(self):
        return self.fetch_attachment(ATTACHMENT_NAME)
        
    def save(self, *args, **kwargs):
        # we have to override this because XFormError does too 
        self["doc_type"] = "SubmissionErrorLog" 
        # and we can't use super for the same reasons XFormError 
        XFormInstance.save(self, *args, **kwargs)

    @classmethod
    def from_instance(cls, instance, message):
        """
        Create an instance of this record from a submission body
        """
        error = SubmissionErrorLog(received_on=datetime.datetime.utcnow(),
                                   md5=hashlib.md5(instance).hexdigest(),
                                   problem=message)
        error.save()
        error.put_attachment(instance, ATTACHMENT_NAME)
        error.save()
        return error
    
