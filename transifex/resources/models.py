# -*- coding: utf-8 -*-
"""
String Level models.
"""

import datetime, sys, re

from hashlib import md5
from django.conf import settings
from django.core.cache import cache
from django.db import models, transaction
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from django.utils.hashcompat import md5_constructor
from django.utils import simplejson as json
from django.contrib.auth.models import User, AnonymousUser

from transifex.languages.models import Language
from transifex.projects.models import Project
from transifex.storage.models import StorageFile
from transifex.txcommon.db.models import CompressedTextField
from transifex.txcommon.log import logger
from transifex.resources.utils import invalidate_template_cache

class ResourceManager(models.Manager):

    def for_user(self, user):
        """
        Filter available resources based on the user doing the query. This
        checks permissions and filters out private resources that the user
        doesn't have access to.
        """
        return Resource.objects.filter(
            project__in=Project.objects.for_user(user))

class Resource(models.Model):
    """
    A translatable resource, such as a document, a set of strings, etc.
    
    This is roughly equivalent to a POT file, string stream, or some other
    strings grouped together as a single translatable element.
    
    The resource is the rough equivalent to the deprecated 'Component' object,
    but with an important difference: a resource can only have one "source file"
    whereas the Component was able to encapsulate multiple ones.
    
    A resource is always related to a project.
    """

    # Short identifier to be used in the API URLs
    slug = models.SlugField(_('Slug'), max_length=50,
        help_text=_("A short label to be used in the URL, containing only "
                    "letters, numbers, underscores or hyphens."))
    name = models.CharField(_('Name'), max_length=255, null=False, blank=False,
        help_text=_("A descriptive name unique inside the project."))

    # i18n related fields
    source_file = models.ForeignKey(StorageFile, verbose_name=_("Source file"),
        blank=True, null=True,
        help_text=_("A local file used to extract the strings to be "
                    "translated."))
    i18n_type = models.CharField(_('I18n type'), max_length=20, editable=False,
        choices=((k,settings.I18N_METHODS[k]['description']) for k,v in settings.I18N_METHODS.items()),
        help_text=_("The type of i18n method used in this resource (%s)") %
                    ', '.join(settings.TRANS_CHOICES.keys()))
    accept_translations = models.BooleanField(_('Accepting translations?'),
        blank=False, null=False, default=True,
        help_text=_('Is this resource accepting submissions of translations?'))
    total_entities = models.IntegerField(_('Total source entities'),
        blank=False, null=False, editable=False, default=0,
        help_text=_('The number of source strings in this translation'
            ' resource.'))
    wordcount = models.IntegerField(_('Number of words in source entities.'),
        blank=False, null=False, editable=False, default=0,
        help_text=_('The number of words contained in the source entities in'
            ' this translation resource.'))

    # Timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_update = models.DateTimeField(auto_now=True, editable=False)

    # Foreign Keys
    source_language = models.ForeignKey(Language,
        verbose_name=_('Source Language'), blank=False, null=False,
        help_text=_("The source language of this Resource."))
    project = models.ForeignKey(Project, verbose_name=_('Project'),
        blank=False, null=True, related_name='resources',
        help_text=_("The project containing the translation resource."))

    # Managers
    objects = ResourceManager()

    def __unicode__(self):
        return u'%s (%s)' % (self.slug, self.project)

    def __repr__(self):
        return "<Resource: %s>" % self.slug

    class Meta:
        unique_together = ('slug', 'project',)
        verbose_name = _('resource')
        verbose_name_plural = _('resources')
        ordering  = ['name',]
        order_with_respect_to = 'project'
        models.get_latest_by = 'created'


    def save(self, *args, **kwargs):
        """
        Do some etxra processing along with the actual save to db.
        """
        # If object is new (aka created=True)
        created=False
        if not self.pk:
            created=True
        # Save the object
        super(Resource, self).save(*args, **kwargs)
        # Create the team language stat objects
        if created:
            RLStats = models.get_model('rlstats', 'RLStats')
            Team = models.get_model('teams', 'Team')
            for team in Team.objects.filter(project=self.project):
                lang = team.language
                RLStats.objects.get_or_create(resource=self, language=lang)

        invalidate_template_cache("project_resource_details",
            self.project.slug, self.slug)
        invalidate_template_cache("resource_details",
            self.project.slug, self.slug)

    def delete(self, *args, **kwargs):
        """
        Do some extra processing along with the actual delete to db.
        """
        # Import is here to avoid circular imports
        from transifex.resources.handlers import invalidate_stats_cache

        invalidate_stats_cache(self, self.source_language)
        RLStats = models.get_model('rlstats', 'RLStats')
        RLStats.objects.filter(resource=self).delete()
        super(Resource, self).delete(*args, **kwargs)

    def update_total_entities(self, total_entities=None):
        """
        Return the total number of SourceEntity objects to be translated.
        """
        if total_entities:
            self.total_entities = total_entities
        else:
            self.total_entities = SourceEntity.objects.filter(resource=self).values('id').count()

        self.save()

    def update_wordcount(self):
        """
        Return the number of words which need translation in this resource.

        The counting of the words uses the Translation objects of the SOURCE
        LANGUAGE as set of objects. This function does not count the plural
        strings!
        """
        wc = 0
        source_trans = Translation.objects.filter(source_entity__id__in=
            SourceEntity.objects.filter(resource=self).values('id'))
        for t in source_trans:
            if t:
                wc += t.wordcount
        self.wordcount = wc
        self.save()

    @models.permalink
    def get_absolute_url(self):
        return ('resource_detail', None,
            { 'project_slug': self.project.slug, 'resource_slug' : self.slug })

    @property
    def full_name(self):
        """
        Return a simple string without spaces identifying the resource.

        Can be used instead of __unicode__ to create files on disk, URLs, etc.
        """        
        return "%s.%s" % (self.project.slug, self.slug)

    @property
    def entities(self):
        """Return the resource's translation entities."""
        return SourceEntity.objects.filter(resource=self)

class SourceEntityManager(models.Manager):

    def for_user(self, user):
        """
        Filter available source entities based on the user doing the query. This
        checks permissions and filters out private source entites that the user
        doesn't have access to.
        """
        return SourceEntity.objects.filter(
            resource__in=Resource.objects.for_user(user))

class SourceEntity(models.Model):
    """
    A representation of a source string which is translated in many languages.
    
    The SourceEntity is pointing to a specific Resource and it is uniquely 
    defined by the string, context and resource fields (so they are unique
    together).
    """
    string = models.TextField(_('String'), blank=False, null=False,
        help_text=_("The actual string content of source string."))
    string_hash = models.CharField(_('String Hash'), blank=False, null=False,
        max_length=32, editable=False,
        help_text=_("The hash of the translation string used for indexing"))
    context = models.CharField(_('Context'), max_length=255,
        null=False, default="",
        help_text=_("A description of the source string. This field specifies"
                    "the context of the source string inside the resource."))
    position = models.IntegerField(_('Position'), blank=True, null=True,
        help_text=_("The position of the source string in the Resource."
                    "For example, the specific position of msgid field in a "
                    "po template (.pot) file in gettext."))
    #TODO: Decision for the following
    occurrences = models.TextField(_('Occurrences'), max_length=1000,
        blank=True, editable=False, null=True,
        help_text=_("The occurrences of the source string in the project code."))
    flags = models.TextField(_('Flags'), max_length=100,
        blank=True, editable=False,
        help_text=_("The flags which mark the source string. For example, if"
                    "there is a python formatted string this is marked as "
                    "\"#, python-format\" in gettext."))
    developer_comment = models.TextField(_('Flags'), max_length=1000,
        blank=True, editable=False,
        help_text=_("The comment of the developer."))

    pluralized = models.BooleanField(_('Pluralized'), blank=False,
        null=False, default=False,
        help_text=_("Identify if the entity is pluralized ot not."))

    # Timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_update = models.DateTimeField(auto_now=True, editable=False)

    # Foreign Keys
    # A source string must always belong to a resource
    resource = models.ForeignKey(Resource, verbose_name=_('Resource'),
        blank=False, null=False, related_name='source_entities',
        help_text=_("The translation resource which owns the source string."))

    objects = SourceEntityManager()

    def __unicode__(self):
        return self.string

    class Meta:
        unique_together = (('string_hash', 'context', 'resource'),)
        verbose_name = _('source string')
        verbose_name_plural = _('source strings')
        ordering = ['string', 'context']
        order_with_respect_to = 'resource'
        get_latest_by = 'created'

    def save(self, *args, **kwargs):
        """
        Do some exra processing before the actual save to db.
        """
        context = self.context
        # This is for sqlite support since None objects are treated as strings
        # containing 'None'
        if not context or context == 'None':
            context = ""

        # Calculate new hash
        self.string_hash = md5_constructor(':'.join([self.string,
            context]).encode('utf-8')).hexdigest()

        super(SourceEntity, self).save(*args, **kwargs)

    def get_translation(self, lang_code, rule=5):
        """Return the current active translation for this entity."""
        try:
            return self.translations.get(language__code=lang_code, rule=rule)
        except Translation.DoesNotExist:
            return None


class TranslationManager(models.Manager):
    def by_source_entity_and_language(self, string,
            source_code='en', target_code=None):
        """
        Return the results of searching, based on a specific source string and
        maybe on specific source and/or target language.
        """
        source_entities = []

        source_entities = SourceEntity.objects.filter(string=string,)

        # If no target language given search on any target language.
        if target_code:
            language = Language.objects.by_code_or_alias(target_code)
            results = self.filter(
                        source_entity__in=source_entities, language=language)
        else:
            results = self.filter(source_entity__in=source_entities)
        return results

    def by_string_and_language(self, string, source_code='en', target_code=None):
        """
        Search translation for source strings queries and only in Public projects!
        """
        query = models.Q()
        for term in string.split(' '):
            query &= models.Q(string__icontains=term)

        source_language = Language.objects.by_code_or_alias(source_code)

        # If no target language given search on any target language.
        if target_code:
            language = Language.objects.by_code_or_alias(target_code)
            results =  self.filter(language=language,
                source_entity__resource__project__in=Project.public.all(),
                source_entity__id__in=self.filter(query, language=source_language).values_list(
                    'source_entity', flat=True))
        else:
            results =  self.filter(
                source_entity__resource__project__in=Project.public.all(),
                source_entity__id__in=self.filter(query, language=source_language).values_list(
                    'source_entity', flat=True))
        return results

class Translation(models.Model):
    """
    The representation of a live translation for a given source string.
    
    This model encapsulates all the necessary fields for the translation of a 
    source string in a specific target language. It also contains a set of meta
    fields for the context of this translation.
    """

    string = models.TextField(_('String'), blank=False, null=False,
        help_text=_("The actual string content of translation."))
    string_hash = models.CharField(_('String Hash'), blank=False, null=False,
        max_length=32, editable=False,
        help_text=_("The hash of the translation string used for indexing"))
    rule = models.IntegerField(_('Plural rule'), blank=False,
        null=False, default=5,
        help_text=_("Number related to the plural rule of the translation. "
                    "It's 0=zero, 1=one, 2=two, 3=few, 4=many and 5=other. "
                    "For translations that have its entity not pluralized, "
                    "the rule must be 5 (other)."))

    # Timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_update = models.DateTimeField(auto_now=True, editable=False)

    # Foreign Keys
    # A source string must always belong to a resource
    source_entity = models.ForeignKey(SourceEntity,
        verbose_name=_('Source String'),
        related_name='translations',
        blank=False, null=False,
        help_text=_("The source string this translation string translates."))

    language = models.ForeignKey(Language,
        verbose_name=_('Target Language'),blank=False, null=True,
        help_text=_("The language in which this translation string is written."))

    user = models.ForeignKey(User,
        verbose_name=_('Committer'), blank=False, null=True,
        help_text=_("The user who committed the specific translation."))

    #TODO: Managers
    objects = TranslationManager()

    def __unicode__(self):
        return self.string

    class Meta:
        unique_together = (('source_entity', 'language', 'rule'),)
        verbose_name = _('translation string')
        verbose_name_plural = _('translation strings')
        ordering  = ['string',]
        order_with_respect_to = 'source_entity'
        get_latest_by = 'created'

    def save(self, *args, **kwargs):
        """
        Do some exra processing before the actual save to db.
        """
        # encoding happens to support unicode characters
        self.string_hash = md5(self.string.encode('utf-8')).hexdigest()
        super(Translation, self).save(*args, **kwargs)

    @property
    def wordcount(self):
        """
        Return the number of words for this translation string.
        """
        # use None to split at any whitespace regardless of length
        # so for instance double space counts as one space
        return len(self.string.split(None))

class Template(models.Model):
    """
    Source file template for a specific resource.

    This model holds the source file template in a compressed textfield to save
    space in the database. All translation strings are changed with the md5
    hashes of the SourceEntity string which enables us to do a quick search and
    replace each time we want to recreate the file.
    """

    content = CompressedTextField(null=False, blank=False,
        help_text=_("This is the actual content of the template"))
    resource = models.ForeignKey(Resource,
        verbose_name="Resource",unique=True,
        blank=False, null=False,related_name="source_file_template",
        help_text=_("This is the template of the imported source file which is"
            " used to export translation files from the db to the user."))

    class Meta:
        verbose_name = _('Template')
        verbose_name_plural = _('Templates')
        ordering = ['resource']

from transifex.resources.formats.qt import LinguistHandler # Qt4 TS files
#from resources.formats.java import JavaPropertiesParser # Java .properties
#from resources.formats.apple import AppleStringsParser # Apple .strings
#from resources.formats.ruby import YamlParser # Ruby On Rails (broken)
#from resources.formats.resx import ResXmlParser # Microsoft .NET (not finished)
from transifex.resources.formats.pofile import POHandler # GNU Gettext .PO/.POT parser
from transifex.resources.formats.joomla import JoomlaINIHandler # GNU Gettext .PO/.POT parser

PARSERS = [POHandler , LinguistHandler, JoomlaINIHandler ] #, JavaPropertiesParser, AppleStringsParser]
