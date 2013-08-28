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

import os
from hashlib import md5
import shutil, tempfile
import socket

import zc.buildout
import tarfile
import ConfigParser
import sys
from itertools import chain
import re
from ssh import RSAKey
try:
    from ssh import DSAKey
except:
    DSAKey = None
from ssh import SSHConfig
from fabric import api
from fabric.state import output

import time, random
from collective.hostout import relpath
import pkg_resources
from setuptools import package_index
from urllib import pathname2url
import StringIO
import functools
from optparse import OptionParser
try:
    from socksproxy.blocking_socks import SocksServer
    from socksproxy.blocking_socks import SocksHandler as SocksHandlerBase
except:
    SocksServer = None
    SocksHandlerBase = None
import SocketServer
import threading
import select
import logging
from contextlib import closing
#from ProxyHTTPServer import ProxyHTTPRequestHandler
from TinyHTTPProxy import ProxyHandler as ProxyHTTPRequestHandler
import BaseHTTPServer

#logging.basicConfig(level=logging.DEBUG)



"""
1. ensure we are on trunk and up to date somehow.
1. Find any dependencies that need a new release and increment the version and create a distribution
1. create a hostout.cfg which is a repeatable buildout which is pinned for deployment by listing all
of all the eggs.
2. version this + all dev dependencies with a tag so can recover this version.
4. bundle the cfg up + eggs (maybe just dev eggs)
5. send to host
6. setup host it need be
7. overwrite with bundle and build
"""

def clean(lines):
    if lines is None:
        return []
    return [l.strip() for l in lines.split('\n') if l.strip() != '']

_isurl = re.compile('([a-zA-Z0-9+.-]+)://').match

max_name_len = 18

def get_all_extends(cfgfile):
    if _isurl(cfgfile):
        return []

    config = ConfigParser.ConfigParser()
    config.optionxform = str
    config.read([cfgfile])
    files = [cfgfile]
    if not 'buildout' in config.sections():
        return files
    if not 'extends' in config.options('buildout'):
        return files
    extends = chain(*[el.split() for el in clean(config.get('buildout', 'extends'))])
    curdir = os.path.dirname(cfgfile)
    for extend in extends:
        if not _isurl(extend):
            extend = os.path.join(curdir, extend)
        files.extend(get_all_extends(extend))
    return files


class DistributionGenerationException(Exception):
    def __init__(self, path, args):
        self.path = path
        self.args = args
        
    def __str__(self):
        return  "Error releasing egg at %s: No egg found after \n python setup.py %s" % (self.path, self.args)


class HostOut:
    def __init__(self, name, opt, packages, hostouts):

        self.buildout_dir = packages.buildout_location
        self.dist_dir = packages.dist_dir
        self.packages = packages
        self.hostout_package = None
        self.options = opt
        self.hostouts = hostouts

        self.name = name
        self.remote_dir = opt.setdefault('path', '/var/lib/plone/%s'%name)
        if not opt.get('hostname'):
            self.host = name
            self.port = 22
        try:
            self.host, self.port = opt['hostname'].split(':')
            self.port = int(self.port)
        except:
            self.host = opt.get('hostname')
            self.port = 22
            
        self.user = opt.get('buildout-user',opt.get('user'))
        self.password = opt.get('password')
        self.identityfile = opt.get('identity-file')
        shell =  opt.get('shell')
        if shell is not None:
            api.env.shell = shell
        self.start_cmd = opt.get('post-commands')
        self.stop_cmd = opt.get('pre-commands')
        self.extra_config = opt.get('include')
        self.extends = [s.strip() for s in opt.get('extends','').split()] + ['collective.hostout']
        self.buildout_cfg = [p.strip() for p in opt.get('buildout','buildout.cfg').split() if p.strip()]
        self.versions_part = opt.get('versions','versions')
        self.parts = [p.strip() for p in opt.get('parts','').split() if p.strip()]
        self.buildout_cache = opt.get('buildout-cache','')
        opt['download_cache']= "%s/%s" % (self.buildout_cache, 'downloads')
        install_base = os.path.dirname(self.getRemoteBuildoutPath())
        if not self.buildout_cache:
            self.buildout_cache = os.path.join(install_base, 'buildout-cache', self.user)
            opt['buildout-cache'] = self.buildout_cache


        self.fabfiles = [p.strip() for p in opt.get('fabfiles','').split() if p.strip()] 

        #self.packages = opt['packages']
        #dist_dir = os.path.abspath(os.path.join(self.buildout_location,self.dist_dir))
        #if not os.path.exists(dist_dir):
        #    os.makedirs(dist_dir)
        #self.tar = None
        self.sets = []

        self.options['user'] = self.options.get('user') or self.user or 'root'
        self.options['effective-user'] = self.options.get('effective-user') or self.user or 'root'
        self.options['buildout-user'] = self.options.get('buildout-user') or self.user or 'root'
        self.options["no-sudo"] = self.options.get("no-sudo") or False

        self.options["force-python-compile"] = self.options.get("system-python-use-not", self.options.get('force-python-compile', 'False'))
        self.options["force-python-compile"] = self.options["force-python-compile"] in ['True','true','yes','Yes']
        self.options["python-path"] = self.options.get("python-path", os.path.join(install_base, "python"))
        self.options["python-prefix"] = self.options.get("python-prefix", os.path.join(install_base, "python"))
        self.options['tunnel'] = self.options.get("tunnel") or False

        self.firstrun = True

    def getPreCommands(self):
        return self._subRemote(clean(self.stop_cmd))

    def getPostCommands(self):
        return self._subRemote(clean(self.start_cmd))

    def getBuildoutDependencies(self):
        abs = lambda p: os.path.abspath(os.path.join(self.getLocalBuildoutPath(),p))
        return [abs(p) for p in clean(self.extra_config)]

    def getLocalBuildoutPath(self):
        return os.path.abspath(self.packages.buildout_location)

    def getRemoteBuildoutPath(self):
        return self.remote_dir

    def localEggs(self):
        return [e for p,v,e in self.packages.release_eggs().values()]

    def getParts(self):
        return self.parts

    def getDownloadCache(self):
        return "%s/%s" % (self.buildout_cache, 'downloads')
    def getEggCache(self):
        return "%s/%s" % (self.buildout_cache, 'eggs')

    def _subRemote(self, cmds):
        "replace abs localpaths to the buildout with absluote remate buildout paths"
        return [c.replace(self.getLocalBuildoutPath(), self.getRemoteBuildoutPath()) for c in cmds]

#    def getDeployTar(self):
#        return self.packages.getDeployTar()

#    def getHostoutFile(self):
#        #make sure package has generated
#        self.getHostoutPackage()
#        return self.config_file[len(self.packages.buildout_location)+1:]


    def getHostoutFile(self):
        config = ConfigParser.ConfigParser()
        config.optionxform = str
#        config.read([path])
        if 'buildout' not in config.sections():
            config.add_section('buildout')
        files = []
        files = files + self.buildout_cfg
        base = self.buildout_dir
        files = [relpath(file, base) for file in files]+['pinned.cfg']

        config.set('buildout', 'extends', ' '.join(files))
        config.set('buildout', 'eggs-directory', self.getEggCache())
        config.set('buildout', 'download-cache', self.getDownloadCache())
        config.set('buildout', 'newest', 'true')
        if self.getParts():
            config.set('buildout', 'parts', ' '.join(self.getParts()))

        res = StringIO.StringIO()
        config.write(res)
        genconfig = res.getvalue()
        
        if self.options['versionsfile']:
            versions = open(self.options['versionsfile']).read()
            #need to remove the dev eggs
            eggs = self.packages.getPackages()
            for line in versions.split('\n'):
                found = False
                for package in eggs.keys():
                    if line.startswith(package):
                        found = True
                        break
                if not found:
                    genconfig += line+'\n'
        
        return genconfig



    def getHostoutPackage(self):
        "determine all the buildout files that make up this configuration and package them"

        if self.hostout_package is not None:
            return self.hostout_package
        dist_dir = self.packages.dist_dir
        base = self.buildout_dir
        #self.config_file = os.path.join(base,'%s.cfg'%self.name)
        
        #config_file = os.path.abspath(os.path.join(self.packages.buildout_location,self.config_file))
        #if not os.path.exists(config_file):
        #    raise Exception("Invalid config file")

        #config_file = self.buildout_cfg
        files = set()
        for file in self.buildout_cfg:
            files = files.union( set(get_all_extends(file)))
        files = files.union( set(self.getBuildoutDependencies()))
        
        filesAbsolute = [os.path.abspath(f) for f in files]
        filesRelative = [os.path.relpath(f, self.buildout_dir) for f in filesAbsolute]
        filesAbsRel = zip (filesAbsolute, filesRelative)
        self.releaseid = _dir_hash(filesAbsolute)
        
        
        name = '%s/%s_%s.tgz'%(dist_dir,'deploy', self.releaseid)
        self.hostout_package = name
        #if os.path.exists(name):
        #    return name
        #else:
        self.tar = tarfile.open(name,"w:gz")

        for fileAbs, fileRel in filesAbsRel:
            self.tar.add(fileAbs,arcname=fileRel)
            
        self.tar.close()
        return self.hostout_package


    def getIdentityKey(self):
        keyfile = os.path.abspath(os.path.join(self.getLocalBuildoutPath(),'hostout_rsa'))
        keyfile = self.options.get('identity-file', keyfile)
        if not os.path.exists(keyfile):
            key = RSAKey.generate(1024)
            key.write_private_key_file(keyfile)
        else:
            try:
                key = DSAKey.from_private_key_file(keyfile)
            except:
                key = RSAKey.from_private_key_file(keyfile)
        return keyfile, "ssh-rsa %s hostout@hostout" % key.get_base64()

    @property
    def socks_proxy(self):
        " Create a tunnel back to the dev server and start a socks proxy, returning the remote connection string "
        if not self.options.get("tunnel",False):
            return ''
        
        from fabric.state import connections
        transport = connections[api.env.host_string].get_transport()
        if getattr(self,'socks_server',None) is None:
            self.socks_server = SocksProxy(transport, ('127.0.0.1', 7000))

        return '127.0.0.1:7000'

    @property
    def http_proxy(self):
        " Create a tunnel back to the dev server and start a socks proxy, returning the remote connection string "
        if not self.options.get("tunnel",False):
            return ''

        from fabric.state import connections
        transport = connections[api.env.host_string].get_transport()
        if getattr(self,'http_server',None) is None:
            self.http_server = HTTPProxy(transport, ('127.0.0.1', 7001))

        return '127.0.0.1:7001'

    def readsshconfig(self):
        config = os.path.expanduser('~/.ssh/config')
        if not os.path.exists(config):
            return
        f = open(config,'r')
        sshconfig = SSHConfig()
        sshconfig.parse(f)
        f.close()
        host = self.host
        try:
            host,port = host.split(':')
        except:
            port = None
        opt = sshconfig.lookup(host)

        if port is None:
            port = opt.get('port')

        host = opt.get('hostname', host)
        if port:
            host = "%s:%s" % (host,port)
        self.host=host
        if not self.identityfile:
            self.identityfile = opt.get('identityfile', None)
            if self.identityfile:
                self.identityfile = os.path.expanduser(self.identityfile).strip()
        if not self.user:
            self.user=opt.get('user','root')

    def allcmds(self):
        if self.sets:
            return self._allcmds
        fabfiles = [(cmds,fabfile) for cmds,fabfile,pkg in findfabfiles() if pkg in self.extends]
        self.sets.extend( fabfiles)
        
        for fabfile in self.fabfiles:

            #fabric._load_default_settings()
            commands = load_fabfile(fabfile)
            self.sets.append((commands,fabfile))
        self._allcmds = {}
        for commands,fabfile in self.sets:
            self._allcmds.update(commands)
        return self._allcmds


    def resetenv(self, buildoutuser=False):
#        self.readsshconfig()
        api.env.update( self.options )
        #api.env.path = '' #HACK - path == cwd
        if self.password:
            api.env['password']=self.password
        if self.identityfile and os.path.exists(self.identityfile):
            api.env['key_filename']=self.identityfile
        return api.settings( **dict(
                   user=self.user,
                   hosts=[self.host],
                   port=self.port,
                   hostout=self,
                   host_string="%(user)s@%(host)s:%(port)s"%dict(user=self.user,
                                                                 host=self.host,
                                                                 port=self.port)
                   ))

    def runcommand(self, cmd, *cmdargs, **vargs):
        self.allcmds()

        

        #if self.firstrun:
        #    self.resetenv()
        #    self.firstrun = False

        # Let plugins change host or user if they want
        self.inits = [(set.get('initcommand'),fabfile) for set,fabfile in self.sets if 'initcommand' in set]
        for func,fabfile in self.inits:
            func(cmd)

        funcs = [(set.get(cmd),fabfile) for set,fabfile in self.sets if cmd in set]
        if not funcs:
            print >> sys.stderr, "'%(cmd)s' is not a valid command for host '%(host)s'"%locals()
            return

        def superfun(funcs, *cmdargs, **vargs):
            if len(funcs) == 0:
                return None
            func,fabfile = funcs[0]
            
            api.env['superfun'] = functools.partial(superfun, funcs[1:])

            print "Hostout: Running command '%(cmd)s' from '%(fabfile)s'" % dict(cmd=cmd,
                                                                                 fabfile=fabfile)

            #key_filename = api.env.get('identity-file')
            #if key_filename and os.path.exists(key_filename):
            #    api.env.key_filename = key_filename

            #if len(api.env.hosts):
            #    api.env['host'] = api.env.hosts[0]
            #    api.env['host_string']="%(user)s@%(host)s:%(port)s"%api.env
            #else:
            #    api.env['host'] = None
            #    api.env['host_string'] = None
                
            #api.env.cwd = ''
            output.debug = True
            res = api.execute(func,*cmdargs, **vargs)
           
            return res

        with api.settings(hostout=self):
            with self.resetenv():
                callingsuper = api.env.get('superfun',None)
                res = superfun(funcs, *cmdargs, **vargs)
                api.env['superfun'] = callingsuper
        
        return res

#    def __getattr__(self, name):
#        """ call all the methods by this name in fabfiles """
#        if name not in self.allcmds():
#            raise AttributeError()
#        def run(*args, **vargs):
#            return self.runcommand(name, *args, **vargs)
#        return run




import zc.buildout.easy_install
from zc.buildout.buildout import pkg_resources_loc


class Packages:
    """ responsible for packaging the development eggs ready to be released to each host"""

    def __init__(self, buildout):
        
        self.packages = [p for p in buildout.get('packages','').split()]

        self.buildout_location = buildout.get('location','')
        self.dist_dir = buildout.get('dist_dir','')
#        self.versions = dict(config.items('versions'))
        self.tar = None
        dist_dir = os.path.abspath(os.path.join(self.buildout_location,self.dist_dir))
        if not os.path.exists(dist_dir):
            os.makedirs(dist_dir)
        self.dist_dir = dist_dir
        self.local_eggs = {}

    def getDistEggs(self):
        files = os.listdir(self.dist_dir)
        
        eggs = []
        for file in files:
            eggs += pkg_resources.find_distributions(os.path.join(self.dist_dir, file) )
        return dict([(( egg.project_name,egg.version),egg) for egg in eggs])
        #eggs = pkg_resources.Environment(self.dist_dir)
        #return dict([(( egg.project_name,egg.version),egg) for egg in eggs])
        
        
    def getPackages(self):
        res = {}
        for path in self.packages:

            # use buildout to run setup for us
            path = os.path.abspath(path)
            dists = find_distributions(path)
            if dists:
                dist = dists[0]
                res[dist.project_name] = (dist.project_name, dist.version)
        return res


    def release_eggs(self):
        "developer eggs->if changed, increment versions, build and get ready to upload"
        # first get list of deveelop packages we got from recipe
        # for each package
        #
        if self.local_eggs:
            return self.local_eggs

        #python setup.py sdist bdist_egg
 #       tmpdir = tempfile.mkdtemp()
        localdist_dir = tempfile.mkdtemp()
        
        #eggs = self.getDistEggs()
        from setuptools.package_index import interpret_distro_name
        for path in os.listdir(self.dist_dir):
            #path = os.path.join(self.dist_dir, path)
            for dist in interpret_distro_name(self.dist_dir, path, None):
                    pass
                
            #egg = pkg_resources.find_distributions(path, only=False)

        ids = {}
        self.local_eggs = {}
        released = {}
        if self.packages:
            print "Hostout: Preparing eggs for transport"
        for path in self.packages:

            # use buildout to run setup for us
            hash = _dir_hash([path])
            ids[hash]=path
            path = os.path.abspath(path)
            dist = find_distributions(path)
            egg = None
            #if len(dist):
            #    dist = dist[0]
        #       for file in eggs:
        #           if file.count(hash):
        #               egg = os.path.join(self.dist_dir, file)
        #               break
            if False and egg:
                #HACK should get out of zip file
                version = dist.version
                #if 'collective.recipe.filestorage' in dist.project_name:
                version += 'dev' not in dist.version and 'dev' or ''
                version += hash not in dist.version and '-'+hash or ''
                self.local_eggs[dist.project_name] = (dist.project_name, version, egg)
            elif os.path.isdir(path):
                print "Hostout: Develop egg %s changed. Releasing with hash %s" % (path,hash)
                args=[path,
                                     'clean',
                                     'egg_info',
                                     '--tag-build','dev_'+hash,
                                     'sdist',
                                     '--formats=zip', #fix bizzare gztar truncation on windows
                                     # 'bdist_egg',
                                     '--dist-dir',
                                     '%s'%localdist_dir,
                                      ]
                self.setup(args = args)
                dist = find_distributions(path)
                
                if not len(dist) or not os.listdir(localdist_dir):
                    raise DistributionGenerationException(path, args)
                dist = dist[0]
                pkg = os.listdir(localdist_dir)[0]
                loc = os.path.join(self.dist_dir, pkg)
                if os.path.exists(loc):
                    os.remove(loc)
                shutil.move(os.path.join(localdist_dir, pkg), self.dist_dir)
                
                self.local_eggs[dist.project_name] = (dist.project_name, dist.version, loc)
                #released[dist.project_name] = dist.version
            else:
#                shutil.copy(path,localdist_dir)
                self.local_eggs[path] = (None, None, path)
        if released:
            env = package_index.PackageIndex('file://'+pathname2url(localdist_dir))

            #eggs = self.getDistEggs()
            for (name,version) in released.items():
                
                req  = pkg_resources.Requirement.parse("%(name)s==%(version)s"%locals())
                env.prescan()
                egg = env.find_packages(req)
                #egg = eggs.get( (name, version) )
                if egg:
                    self.local_eggs[name] = (name, version, egg.location)
                else:
                    raise Exception("%(name)s wasn't generated. See errors above" % locals())


        if self.local_eggs:
            specs = ["\t%s = %s"% (p,v) for p,v,e in self.local_eggs.values()]
            print "Hostout: Eggs to transport:\n%s" % '\n'.join(specs)
        return self.local_eggs


    def developVersions(self):

        self.release_eggs() #ensure we've got self.develop_versions

        specs = {}
        #have to use lower since eggs are case insensitive
        specs.update(dict([(p,v) for p,v,e in self.local_eggs.values()]))

        res = ""
        for name, version in sorted(specs.items()):
            res += "\n%s=%s" % (name,version)
        return res

    def setup(self, args):
        setup = args.pop(0)
        if os.path.isdir(setup):
            setup = os.path.join(setup, 'setup.py')

        #self._logger.info("Running setup script %r.", setup)
        setup = os.path.abspath(setup)


        fd, tsetup = tempfile.mkstemp()
        try:
            os.write(fd, zc.buildout.easy_install.runsetup_template % dict(
                setuptools=pkg_resources_loc,
                setupdir=os.path.dirname(setup),
                setup=setup,
                __file__ = setup,
                ))
            os.spawnl(os.P_WAIT, sys.executable, zc.buildout.easy_install._safe_arg (sys.executable), tsetup,
                      *[zc.buildout.easy_install._safe_arg(a)
                        for a in args])
        finally:
            os.close(fd)
            os.remove(tsetup)


from fabric.tasks import Task,WrappedCallableTask

"""

Plan is override get_hosts and use the roles as keys to the hostouts that have been read
return a set of fake hosts which are the roles.
Then when run is called with the fake host, reset the env to real host, path etc.
each fake host is a key to a hostout config.

"""
class HostoutTask(WrappedCallableTask):
    def __init__(self, func, buildoutuser=False):
        self.func = func
        self.buildoutuser = buildoutuser
        WrappedCallableTask.__init__(self, func)
 #   def get_hosts(self, arg_hosts, arg_roles, arg_exclude_hosts, env=None):
 #       import pdb; pdb.set_trace()
 #       return arg_roles
    def run(self, *args, **kwargs):
        hostout = api.env.hostouts.get(api.env.host_string)
        with hostout.resetenv(self.buildoutuser):
            return self.func(*args, **kwargs)

def read_config(cfgfile):
    allhosts = {}
    if type(cfgfile) == type(dict()):
        packages = Packages(cfgfile)
        for section,options in cfgfile.get('hostouts').items():
            hostout = HostOut(section, options, packages, allhosts)
            allhosts[section] = hostout
    else:
        config = ConfigParser.ConfigParser()
        config.optionxform = str
        config.read([cfgfile])
    #    buildout = Buildout(config.get('buildout','buildout'),[])
        packages = Packages(dict(config.items('buildout')))
        #eggs = packages.release_eggs()
        #

        for section in [s for s in config.sections() if s not in ['buildout', 'versions']]:
            options = dict(config.items(section))

            hostout = HostOut(section, options, packages, allhosts)
            allhosts[section] = hostout

    api.env['hostouts'] = allhosts


def main(cfgfile, args):
    "execute the fabfile we generated"

    parser = OptionParser()
    parser.add_option("-c", "--configuration", dest="cfgfile", default=cfgfile,
                      help="hostout configuration file", metavar="FILE")
    
    (options, args) = parser.parse_args()
    cfgfile = options.cfgfile.strip()

    read_config(cfgfile)
    allhosts = api.env.hostouts

    from fabric.main import extract_tasks
    import fabfile
    extract_tasks([('hostout',fabfile)])


    # cmdline is bin/hostout host1 host2 ... cmd1 cmd2 ... arg1 arg2...
    cmds = []
    cmdargs = []
    hosts = []
    pos = 'hosts'
    for arg in args + [None]:
        if pos == 'hosts':
            if arg in allhosts:
                hosts += [(arg,allhosts[arg])]
                continue
            elif arg == 'all':
                hosts = allhosts.items()
            else:
                pos = 'cmds'            
                # get all cmds
                allcmds = {'deploy':None}
                for host,hostout in hosts:
                    hostout.readsshconfig()
                    allcmds.update(hostout.allcmds())
        if pos == 'cmds':
            #if arg == 'deploy':
            #    cmds += ['predeploy','uploadeggs','uploadbuildout','buildout','postdeploy']
            #    continue
            #el
            if arg in allcmds:
                cmds += [arg]
                continue
            pos = 'args'

        if pos == 'args' and arg is not None:
            cmdargs += [arg]


    if not hosts or not cmds:
        print >> sys.stderr, "cmdline is: bin/hostout host1 [host2...] [all] cmd1 [cmd2...] [arg1 arg2...]"
    if not hosts:
        print >> sys.stderr, "Valid hosts are: %s"% ' '.join(allhosts.keys())
    elif not cmds:
        print >> sys.stderr, "Valid commands are:"
        max_name_len = reduce(lambda a,b: max(a, len(b)), allcmds.keys(), 0)
        cmds = allcmds.items()
        cmds.sort(lambda x,y: cmp(x[0], y[0]))
        for name, fn in cmds:
            print >> sys.stderr, '  ', name.ljust(max_name_len),
            if fn.__doc__:
                print >> sys.stderr, ':', fn.__doc__.splitlines()[0]
            else:
                print >> sys.stderr, ''
    else:
        try:
            for host, hostout in hosts:
                for cmd in cmds:
                    with api.settings(host=host):
                        if cmd == cmds[-1]:
                            res = hostout.runcommand(cmd, *cmdargs)
                        else:
                            res = hostout.runcommand(cmd)
            print("Return value: %s"%res)
            sys.exit(res is False)
        except SystemExit:
            # a number of internal functions might raise this one.
            raise
        except KeyboardInterrupt:
            print("Stopped.")
        #    except:
        #        sys.excepthook(*sys.exc_info())
        #        # we might leave stale threads if we don't explicitly exit()
        #        return False
#        finally:
#            #disconnect_all()
#           pass



def is_task(tup):
    """
    Takes (name, object) tuple, returns True if it's a non-Fab public callable.
    """
    name, func = tup
    return (
        callable(func)
        and not name.startswith('_')
    )


#
# Use setuptools entry points to find the fabfiles in this env
#
def findfabfiles():
    from pkg_resources import iter_entry_points
    
    fabfiles = []
    for ep in iter_entry_points(
        group='fabric',
        # Use None to get all entry point names
        name=None,
    ):
        imported = ep.load()
	pkg = ep.dist.project_name
        funcs = dict(filter(is_task, vars(imported).items()))
        fabfiles.append( (funcs, ep.module_name, pkg) )
        # ep.name doesn't matter
    #print fabfiles
    return fabfiles


# Fabric load_fabfile uses __import__ which doesn't always load from path    
import imp
def load_fabfile(filename, **kwargs):
    """
    Load up the given fabfile.
    
    This loads the fabfile specified by the `filename` parameter into fabric
    and makes its commands and other functions available in the scope of the 
    current fabfile.
    
    If the file has already been loaded it will not be loaded again.
    
    May take an additional `fail` keyword argument with one of these values:
    
     * ignore - do nothing on failure
     * warn - print warning on failure
     * abort - terminate fabric on failure
    
    Example:
    
        load("conf/production-settings.py")
    
    """
    if not os.path.exists(filename):
        raise Exception("Load failed:\n" 
            "File not found: " + filename)
        return
    
    #if filename in _LOADED_FABFILES:
    #    return
    #_LOADED_FABFILES.add(filename)
    
    #execfile(filename, _new_namespace(), captured)
    imported = imp.load_source(filename.replace('/','.'), filename)
    return dict(filter(is_task, vars(imported).items()))


def uuid( *args ):
  """
    Generates a universally unique ID.
    Any arguments only create more randomness.
  """
  t = long( time.time() * 1000 )
  r = long( random.random()*100000000000000000L )
  try:
    a = socket.gethostbyname( socket.gethostname() )
  except:
    # if we can't get a network address, just imagine one
    a = random.random()*100000000000000000L
  data = str(t)+' '+str(r)+' '+str(a)+' '+str(args)
  data = md5(data).hexdigest()
  return data

ignore_directories = '.svn', 'CVS', 'build', '.git'
ignore_files = ['PKG-INFO']
def _dir_hash(paths):
    hash = md5()
    for path in paths:
        if os.path.isdir(path):
            walked = os.walk(path)
            #find_sources(path)
        else:
            walked = [(os.path.dirname(path), [], [os.path.basename(path)])]
        for (dirpath, dirnames, filenames) in walked:
            dirnames[:] = [n for n in dirnames if not (n in ignore_directories or n.endswith('.egg-info'))]
            filenames[:] = [f for f in filenames
                        if not (f in ignore_files or f.endswith('pyc') or f.endswith('pyo'))]
            hash.update(' '.join(dirnames))
            hash.update(' '.join(filenames))
            for name in filenames:
                hash.update(open(os.path.join(dirpath, name)).read())
    import base64
    hash = base64.urlsafe_b64encode(hash.digest()).strip()
    hash = hash.replace('_','-').replace('=','')
    return hash

from setuptools.command.egg_info import manifest_maker

def find_sources(path):
        dist = find_distributions(path)[0]
        mm = manifest_maker(dist)
        mm.manifest = None
        mm.run()
        return mm.filelist
    

def find_distributions(path):
        #HACK: need to parse setup.py instead assuming src
        return [d for d in pkg_resources.find_distributions(path, only=True)] + \
            [d for d in pkg_resources.find_distributions(os.path.join(path,'src'), only=True)]

def getVersion(path):
        "Test to see if we already have a release of this developer egg"
        dist = [d for d in pkg_resources.find_distributions(path, only=True)]
        dist = dist[0]

        return dist.version



def asbuildoutuser():
    
    kwargs = {"user": api.env.hostout.options['buildout-user']}
    
    
    # Select Authentication method
    password = api.env.hostout.options.get("buildout-password")
    if password:
        kwargs["password"] = password
    else:
        ifile = api.env.get('identity-file')
        if ifile and os.path.exists(ifile):
                kwargs["key_filename"] = ifile

    # we need to reset the host_string
    kwargs['host'] = api.env.hosts[0]
    kwargs['port'] = api.env.port
    kwargs['host_string']="%(user)s@%(host)s:%(port)s"%kwargs

    return api.settings (**kwargs)

class buildoutuser(object):

    def __init__(self, f):
        self.f = f

    def __call__(self, *args, **vargs):
        user = api.env.user
        host_string = api.env.host_string
        api.env.user = api.env.hostout.options['buildout-user']
        key_filename = api.env.key_filename
        password = getattr(api.env, "password")
        
        
        buildoutpass = api.env.hostout.options.get("buildout-password")
        passSet = False
        if buildoutpass:
            api.env.password = buildoutpass
            passSet = True

        else:
            ifile = api.env.get('identity-file')
            if ifile and os.path.exists(ifile):
                api.env.key_filename = ifile
        
        
        #this will reset the connection
        api.env['host_string']="%(user)s@%(host)s:%(port)s"%api.env
        self.f(*args, **vargs)
        api.env.user = user
        api.env.host_string = host_string
        api.env['key_filename'] = key_filename

        if passSet:
            if password != None:
                api.env.password = password
            else:
                del api.env.password




if SocksServer is not None:

    class SocksHandler(SocksHandlerBase):
        def setup(self):
            SocksHandlerBase.setup(self)
            # stupid paramiko doesn't implement closed
            self.rfile.closed = property(lambda self: self._closed)
            self.wfile.closed = property(lambda self: self._closed)

class HTTPProxyHandler(ProxyHTTPRequestHandler):
    def setup(self):
        ProxyHTTPRequestHandler.setup(self)
        # stupid paramiko doesn't implement closed
        self.rfile.closed = property(lambda self: self._closed)
        self.wfile.closed = property(lambda self: self._closed)

class TunneledServer(object):
    def __init__(self, transport, listen_addr, handler):
        self.transport = transport
        # skip SocksServer so we can add our own proxy
        SocketServer.ThreadingTCPServer.__init__(self, listen_addr, handler)
        self.socket = transport
        self.logger = logging.getLogger ("TinyHTTPProxy")
        self.logger.setLevel (logging.INFO)

    def server_bind(self):
        server,port = self.server_address
        self.transport.request_port_forward(server, port, self.handler)

    def server_activate(self):
        pass

    def get_request(self):
        return self.socket, self.client_address

    def handler(self, channel, origin, server):
        " handler from transport.request_port_forward "
        self.socket = channel
        self.client_address = origin
        self._handle_request_noblock()

if SocksServer is not None:
    class SocksProxy(TunneledServer, SocksServer):
        def __init__(self, transport, listen_addr):
            TunneledServer.__init__(self, transport, listen_addr, SocksHandler)

class HTTPProxy(TunneledServer, SocketServer.ThreadingTCPServer, BaseHTTPServer.HTTPServer):
    def __init__(self, transport, listen_addr):
        TunneledServer.__init__(self, transport, listen_addr, HTTPProxyHandler)




