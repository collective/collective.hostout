##############################################################################
#
# Copyright (c) 2006 Zope Corporation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################

import logging, os,  tempfile, urllib2, urlparse
import setuptools.archive_util
import datetime
import zc.buildout
import zc.recipe.egg
from os.path import join
from os.path import dirname, abspath
import ConfigParser
from zc.buildout.buildout import Options, _recipe, _install_and_load
import pkg_resources
import sys


class Recipe:

    def __init__(self, buildout, name, options):

        #self.egg = zc.recipe.egg.Egg(buildout, options['recipe'], options)
        
        self.name, self.options, self.buildout = name, options, buildout
        
        if not buildout['buildout'].get('hostout-main'):
            buildout['buildout']['hostout-main'] = name
            options['mainhostout'] = self.name
            #get all recipes here to make sure we're the last called
            self.getAllRecipes()



        self.buildout_dir = self.buildout.get('buildout').get('directory','')
        self.download_cache = self.buildout['buildout'].get('download-cache','')
        self.install_from_cache = self.buildout['buildout'].get('install-from-cache','')
        self.options['versions'] = self.buildout['buildout'].get('versions','versions')
        self.options['location'] = os.path.join(
            self.buildout['buildout'].get('parts-directory',''),
            'hostout',
            )
        self.optionsfile = join(self.options['location'],'hostout.cfg')

        self.fabfiles = []
        self.subrecipes = []
        self.subpackages = []
        self.extends(options, [])


        self.options.setdefault('dist_dir','dist')
        self.options.setdefault('buildout','buildout.cfg')
        self.options.setdefault('user','') #get default from .ssh/config
        self.options.setdefault('hostname',name) #get default from .ssh/config
        #idfile = os.path.join(self.buildout_dir,"%s_key"%options.get('hostname'))
        #self.options.setdefault('identity-file',self.options.get('identity_file',idfile))
        self.options.setdefault('buildout-user',self.options['user'])
        self.options.setdefault('effective-user',self.options['buildout-user'])
        self.options.setdefault('buildout-group','buildout')
        self.options.setdefault('host', '')
        self.options.setdefault('password','')
        self.options.setdefault('post-commands', self.options.get('start_cmd',''))
        self.options.setdefault('pre-commands', self.options.get('stop_cmd', ''))
        self.options.setdefault('include', self.options.get('extra_config',''))
        self.options.setdefault('parts','')
        self.options.setdefault('versionsfile','hostoutversions.cfg')
        default_path = '~%s/buildout'%self.options['user']
        self.options.setdefault('path', self.options.get('remote_path','/var/buildout/%s'%name))
#        self.extra_config = [s.strip() for s in self.options.get('extra_config','').split('\n') if s.strip()]
        self.options.setdefault('buildout_location',self.buildout_dir)

        self.options['arguments']="'%s',sys.argv[1:]"%self.optionsfile
        self.options['scripts'] = 'hostout'
        self.options['eggs'] = '\n'.join(self.options.get('eggs','').split() +
                                         ['collective.hostout'])

        version = '.'.join([str(i) for i in sys.version_info])
        self.options['python-version'] = options.get('python-version', version)


    def extends(self, options, seen):

        extends = [e.strip() for e in options.get('extends','').split() if e.strip()]
        for extension in extends:
            if extension in seen:
                continue
            seen.append(extension)

            part = self.buildout.get(extension)
            if part is None:
                # try interpreting extends as recipe
                eopts = {}
#                eopts.update(options)
                eopts['recipe'] = extension
                #buildout._raw[extension] = eopts #dodgy hack since buildout.__setitem__ not implemented
                #part = buildout.get(extension)
                #part = Options(buildout, name, eopts)
                #part._initialize()
                reqs, entry = _recipe(eopts)
                recipe_class = _install_and_load(reqs, 'zc.buildout', entry, self.buildout)
                recipe = recipe_class(self.buildout, self.name, self.options)
                self.subrecipes.append(recipe)
                egg = extension.split(':')[0]
                
                main = self.buildout['buildout']['hostout-main']
                mainpart = self.buildout.get(main)
                if mainpart:
                    mainpart['eggs'] = '\n'.join(mainpart.get('eggs','').split() + [egg])

                continue
            else:
                self.extends(part, seen)
                for key in part:
                    if key in ['fabfiles', 'pre-commands', 'post-commands']:
                        fabfiles = part[key].split('\n')
                        self.options[key] = '\n'.join(self.options.get(key, '').split()+fabfiles)
                    elif key not in self.options.keys():
                        self.options[key] = part[key]
        return seen


    def install(self):

        installed = []

        for recipe in self.subrecipes:
            installed = recipe.install()
            if installed is None:
                installed = []
            elif isinstance(installed, basestring):
                installed = [installed]


        logger = logging.getLogger(self.name)

        location = self.options['location']

        if not os.path.exists(location):
            os.mkdir(location)


        config = ConfigParser.ConfigParser()
        config.read(self.optionsfile)

        fp = open(self.optionsfile, 'w+')
        config.write(fp)
        fp.close()
        self.update()
        
        if self.options.get('mainhostout') is not None:
            self.script = zc.recipe.egg.Scripts(self.buildout, self.options['recipe'], self.options)
            installed = installed +  self.script.install()

        return list(installed) + [self.optionsfile]

    def update(self):
        installed = []
        for recipe in self.subrecipes:
            installed = recipe.install()
            if installed is None:
                installed = []
            elif isinstance(installed, basestring):
                installed = [installed]


        if self.options.get('mainhostout') is not None:
            self.script = zc.recipe.egg.Scripts(self.buildout, self.options['recipe'], self.options)
            installed = installed + self.script.update()

        config = ConfigParser.RawConfigParser()
        config.optionxform = str
        config.read(self.optionsfile)

        if not config.has_section(self.name):
            config.add_section(self.name)
        if not config.has_section('buildout'):
            config.add_section('buildout')
        for name,value in self.options.items():
            config.set(self.name, name, value)
        config.set('buildout','location',self.buildout_dir)
        config.set('buildout','download_cache',self.download_cache)

        if self.options.get('mainhostout') is not None:
            self.writeVersions()
            config.set('buildout', 'bin-directory', self.buildout.get('buildout').get('directory'))
            if self.options['dist_dir']:
                config.set('buildout','dist_dir', self.options['dist_dir'])

            packages = [p.strip() for p in self.buildout.get('buildout').get('develop','').split()]
            packages += [p.strip() for p in self.options.get('packages','').split()]
            if not config.has_section('buildout'):
                config.add_section('buildout')
            config.set('buildout', 'packages', '\n   '.join(packages))
        #self.options.setdefault('develop','')

        fp = open(self.optionsfile, 'w+')
        config.write(fp)
        fp.close()
        
        return installed+ [self.optionsfile] 



    def getAllRecipes(self):
        recipes = []

        for part in [p.strip() for p in self.buildout['buildout'].get('parts','').split()]:
            options = self.buildout.get(part) #HACK
            
            if options is None:
                continue
            if not 'recipe' in options.keys():
                continue
            recipe = options.get('recipe',None)
            if recipe is None:
                continue
            elif ':' in recipe:
                recipe,subrecipe = recipe.split(':')
            recipes.append((part,recipe,options))
        return recipes

    def getVersions(self):
        versions = {}
        for part, recipe, options in self.getAllRecipes():
            egg = zc.recipe.egg.Egg(self.buildout, recipe, options)
            spec, entry = _recipe({'recipe':recipe})
            req = pkg_resources.Requirement.parse(spec)
            recipeegg = pkg_resources.working_set.find(req)
            if "collective.hostout" in spec:
                continue #HACK
            requirements, ws = egg.working_set()
            if recipeegg is None:
                raise Exception ("Could not find version info for recipe %s (%s)" % (spec, part))
            for dist in [recipeegg] + [d for d in ws]:
                if dist is None:
                    raise Exception ("Could not find version info for a dependency of recipe %s" % spec)
                old_version,dep = versions.get(dist.project_name,('',[]))
                #if recipe not in dep:
                #    dep.append(recipe)
                if dist.version != '0.0':
                    versions[dist.project_name] = (dist.version,dep)
        spec = ""
        return versions

    def writeVersions(self):
        versions = self.getVersions()
        
        f = open(self.options['versionsfile'], "w")
        
        f.write("[%(versions)s]\n" % self.options)
        for project_name,info in sorted(versions.items()):
            version,deps = info
            if [d for d in deps if d != project_name]:
               continue 
            if version != '0.0':
                spec='%s = %s' % (project_name,version)+'\n'
            else:
                spec='#%s = %s' % (project_name,version)+'\n'
            f.write( spec )

        for project_name,info in sorted(versions.items()):
            version,deps = info
            spec='\n'
            deps = [d for d in deps if d != project_name]
            if not deps:
               continue 
            for dep in sorted(deps):
                if project_name == dep:
                    continue
                if versions.get(dep):
                    dver, ddeps = versions.get(dep)
                else:
                    dver = 'Unknown'
                spec+='# Required by %s \n' % (dep) #versions[dep][0])
            if version != '0.0':
                spec+='%s = %s' % (project_name,version)+'\n'
            else:
                spec+='#%s = %s' % (project_name,version)+'\n'
                
            f.write(spec)
        f.close()
        

# relpath.py
# R.Barran 30/08/2004

import os

def relpath(target, base=os.curdir):
    """
    Return a relative path to the target from either the current dir or an optional base dir.
    Base can be a directory specified either as absolute or relative to current dir.
    """

    if not os.path.exists(target):
        raise OSError, 'Target does not exist: '+target

    if not os.path.isdir(base):
        raise OSError, 'Base is not a directory or does not exist: '+base

    base_list = (os.path.abspath(base)).split(os.sep)
    target_list = (os.path.abspath(target)).split(os.sep)

    # On the windows platform the target may be on a completely different drive from the base.
    if os.name in ['nt','dos','os2'] and base_list[0] <> target_list[0]:
        raise OSError, 'Target is on a different drive to base. Target: '+target_list[0].upper()+', base: '+base_list[0].upper()

    # Starting from the filepath root, work out how much of the filepath is
    # shared by base and target.
    for i in range(min(len(base_list), len(target_list))):
        if base_list[i] <> target_list[i]: break
    else:
        # If we broke out of the loop, i is pointing to the first differing path elements.
        # If we didn't break out of the loop, i is pointing to identical path elements.
        # Increment i so that in all cases it points to the first differing path elements.
        i+=1

    rel_list = [os.pardir] * (len(base_list)-i) + target_list[i:]
    return os.path.join(*rel_list)

