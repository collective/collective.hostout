import shutil
import sys
import os
import os.path
from fabric import api, contrib
import fabric.contrib.files
import fabric.contrib.project
from collective.hostout.hostout import buildoutuser, asbuildoutuser
from fabric.context_managers import cd
#    , path
from pkg_resources import resource_filename
import tempfile
import tarfile


def run(*cmd):
    """Execute cmd on remote as login user """

    with asbuildoutuser():
        return _run(' '.join(cmd), run_cmd=api.run)


def _run(cmd, run_cmd=api.run):

    if run_cmd == api.sudo and api.env["no-sudo"]:
        raise Exception ("Can not execute sudo command because no-sudo is set.")

    with cd( api.env.path):
        proxy = proxy_cmd()
        if proxy:
            run_cmd("%s %s" % (proxy,cmd))
        else:
            run_cmd(cmd)


def sudo(*cmd):
    """Execute cmd on remote as root user """

    return _run(' '.join(cmd), run_cmd=api.sudo)

def runescalatable(*cmd):
    try:
        with asbuildoutuser():
            return _run(' '.join(cmd))
    except:
        try:
            return _run(' '.join(cmd))
        except:
            return _run(' '.join(cmd), run_cmd=api.sudo)


def requireOwnership (file, user=None, group=None, recursive=False):

    if bool(user) !=  bool(group):  # logical xor
        signature = user or group
        sigFormat = (user and "%U") or "%G"
    else:
        signature = "%s:%s" % (user, group)
        sigFormat = "%U:%G"

    if recursive:
        opt = "-R"
    else:
        opt = ""

    getOwnerGroupCmd = "stat --format=%s '%s'" % (sigFormat, file)
    chownCmd = "chown %(opt)s %(signature)s '%(file)s'" % locals()

    api.env.hostout.runescalatable ('[ `%(getOwnerGroupCmd)s` == "%(signature)s" ] || %(chownCmd)s' % locals())



def put(file, target=None):
    """Recursively upload specified files into the remote buildout folder"""
    if os.path.isdir(file):
        uploads = os.walk(file)
    else:
        path = file.split('/')
        uploads = [('/'.join(path[:-1]), [], [path[-1]])]
    with asbuildoutuser():
        for root, dirs, files in uploads:
            for dir in dirs:
                with cd(api.env.path):
                    api.run('mkdir -p %s'% root +'/'+ dir)
            for file in files:
                file = root + '/' + file
                print file
                if target:
                    targetfile = target + '/' + file
                else:
                    targetfile = file
                if targetfile[0] != '/':
                    targetfile = api.env.path + '/' + targetfile
                api.put(file, targetfile)

def putrsync(dir):
    """ rsync a local buildout folder with the remote buildout """
    with asbuildoutuser():
        parent = '/'.join(dir.split('/')[:-1])
        remote = api.env.path + '/' + parent

        fabric.contrib.project.rsync_project(remote_dir=remote, local_dir = dir)

@buildoutuser
def get(file, target=None):
    """Download the specified files from the remote buildout folder"""
    if not target:
        target = file
    if not file.startswith('/'):
        file = api.env.path + '/' + file
    api.get(file, target)

def deploy():
    "predeploy, uploadeggs, uploadbuildout, buildout and then postdeploy"


    hostout = api.env['hostout']
    hostout.predeploy()
    hostout.uploadeggs()
    hostout.uploadbuildout()
    hostout.buildout()
    hostout.postdeploy()


def predeploy():
    """Perform any initial plugin tasks. Call bootstrap if needed"""

    hasBuildoutUser = True
    hasBuildout = True
    if not (api.env.get("buildout-password") or os.path.exists(api.env.get('identity-file'))):
        hasBuildoutUser = False
    else:
        with asbuildoutuser():
            try:
                api.run("[ -e %s/bin/buildout ]"%api.env.path, pty=True)
            except:
                hasBuildout = False

    if not hasBuildoutUser or not hasBuildout:
        raise Exception ("Target deployment does not seem to have been bootstraped.")

    api.env.hostout.precommands()

    return api.env.superfun()

def precommands():
    "run 'pre-commands' as sudo before deployment"
    hostout = api.env['hostout']
    with cd(api.env.path):
        for cmd in hostout.getPreCommands():
            api.sudo('sh -c "%s"'%cmd)


# Make uploadeggs, uploadbuildout and buildout run independent of each other
# uploadeggs should upload the eggs and write out the versions to a versions file on the host
# uploadbuildout should upload buildout + dependencies but no version pinning
# buildout should upload just the generated cfg which instructs which buildout to r
# un. This step should pin versions
# if buildout is run without uploadeggs then no pinned dev eggs versions exist. in which case need
# to upload dummy pinned versions file.

# buildout will upload file like staging_20100411-23:04:04-[uid].cfg
# which extends=staging.cfg hostoutversions.cfg devpins.cfg

# scenarios
# using buildout only
# use uploadbuildout and buildout
# use uploadeggs and then later buildout

# secondary benifit would be to have a set of files which you could roll back easily to a previous
# buildout version including all the dev eggs.



@buildoutuser
def uploadeggs():
    """Release developer eggs and send to host """

    hostout = api.env['hostout']

    #need to send package. cycledown servers, install it, run buildout, cycle up servers

    dl = hostout.getDownloadCache()
    with api.hide('running', 'stdout', 'stderr'):
        contents = api.run('ls %s/dist' % dl).split()

    for pkg in hostout.localEggs():
        name = os.path.basename(pkg)

        if name not in contents:
            tmp = os.path.join('/tmp', name)
            api.put(pkg, tmp)
            api.run("mv -f %(tmp)s %(tgt)s && "
                "chown %(buildout)s %(tgt)s && "
                "chmod a+r %(tgt)s" % dict(
                    tmp = tmp,
                    tgt = os.path.join(dl, 'dist', name),
                    buildout=api.env.hostout.options['buildout-user'],
                    ))
    # Ensure there is no local pinned.cfg so we don't clobber it
    # Now upload pinned.cfg.
    pinned = "[buildout]\ndevelop=\nauto-checkout=\n[versions]\n"+hostout.packages.developVersions()
    tmp = tempfile.NamedTemporaryFile()
    tmp.write(pinned)
    tmp.flush()
    api.put(tmp.name, api.env.path+'/pinned.cfg')
    tmp.close()

@buildoutuser
def uploadbuildout():
    """Upload buildout pinned version of buildouts to host """
    hostout = api.env.hostout
    buildout = api.env['buildout-user']

    package = hostout.getHostoutPackage()
    tmp = os.path.join('/tmp', os.path.basename(package))
    tgt = os.path.join(hostout.getDownloadCache(), 'dist', os.path.basename(package))

    #api.env.warn_only = True
    if api.run("test -f %(tgt)s || echo 'None'" %locals()) == 'None' :
        api.put(package, tmp)
        api.run("mv %(tmp)s %(tgt)s" % locals() )
        #sudo('chown $(effectiveuser) %s' % tgt)

    user=hostout.options['buildout-user']
    install_dir=hostout.options['path']
    with cd(install_dir):
        api.run('tar -p -xvf %(tgt)s' % locals())
#    hostout.setowners()

@buildoutuser
def buildout(*args):
    """ Run the buildout on the remote server """

    hostout = api.env.hostout
    hostout_file=hostout.getHostoutFile()

    #upload generated cfg with hostout versions
    hostout.getHostoutPackage() # we need this work out releaseid
    filename = "%s-%s.cfg" % (hostout.name, hostout.releaseid)

    with cd(api.env.path):
        tmp = tempfile.NamedTemporaryFile()
        tmp.write(hostout_file)
        tmp.flush()
        api.put(tmp.name, api.env.path+'/'+filename)
        tmp.close()

            #if no pinned.cfg then upload empty one
        if not contrib.files.exists('pinned.cfg'):
            pinned = "[buildout]"
            contrib.files.append(pinned, 'pinned.cfg')
        #run generated buildout
#        api.run('%s bin/buildout -c %s -t 1900 %s' % (proxy_cmd(), filename, ' '.join(args)))
        api.run('%s bin/buildout -c %s %s' % (proxy_cmd(), filename, ' '.join(args)))

        # Update the var dir permissions to add group write
        api.run("find var -exec chmod g+w {} \; || true")

def sudobuildout(*args):
    hostout = api.env.get('hostout')
    hostout.getHostoutPackage() # we need this work out releaseid
    filename = "%s-%s.cfg" % (hostout.name, hostout.releaseid)
    with cd(api.env.path):
        api.sudo('bin/buildout -c %s %s' % (filename, ' '.join(args)))


def postdeploy():
    """Perform any final plugin tasks """

    hostout = api.env.get('hostout')
    #hostout.setowners()

    hostout.getHostoutPackage() # we need this work out releaseid
    filename = "%s-%s.cfg" % (hostout.name, hostout.releaseid)
    sudoparts = ' '.join(hostout.options.get('sudo-parts','').split())
    if sudoparts:
        with cd(api.env.path):
            api.sudo('bin/buildout -c %(filename)s install %(sudoparts)s' % locals())


    with cd(api.env.path):
        for cmd in hostout.getPostCommands():
            api.sudo('sh -c "%s"'%cmd)


def bootstrap():
    """ Install packages and users needed to get buildout running """
    hostos = api.env.get('hostos','').lower()
    version = api.env['python-version']
    major = '.'.join(version.split('.')[:2])
    majorshort = major.replace('.','')
    d = dict(major=major)

    if not hostos:
        hostos = api.env.hostout.detecthostos().lower()

    cmd = getattr(api.env.hostout, 'bootstrap_users_%s'%hostos, api.env.hostout.bootstrap_users)
    cmd()

    python = 'python%(major)s' % d
    #if api.env.get("python-path"):
    pythonpath = os.path.join (api.env.get("python-path"),'bin')
    python = "PATH=\$PATH:\"%s\"; %s" % (pythonpath, python)

    try:
        with asbuildoutuser():
            #with cd(api.env["python-prefix"]+'/bin'):
                api.run(python + " -V")
    except:
        if api.env.get('force-python-compile'):
            api.env.hostout.bootstrap_python_buildout()
        else:
            try:
                cmd = getattr(api.env.hostout, 'bootstrap_python_%s'%hostos, api.env.hostout.bootstrap_python)
            except:
                cmd = api.env.hostout.bootstrap_python_buildout

            cmd()

    if api.env.get('force-python-compile'):
        api.env.hostout.bootstrap_buildout()
    else:
        cmd = getattr(api.env.hostout, 'bootstrap_buildout_%s'%hostos, api.env.hostout.bootstrap_buildout)
        cmd()


def setowners():
    """ Ensure ownership and permissions are correct on buildout and cache """
    hostout = api.env.get('hostout')
    buildout = api.env['buildout-user']
    effective = api.env['effective-user']
    buildoutgroup = api.env['buildout-group']
    owner = buildout


    path = api.env.path
    bc = hostout.buildout_cache
    dl = hostout.getDownloadCache()
    dist = os.path.join(dl, 'dist')
    ec = hostout.getEggCache()
    var = os.path.join(path, 'var')

    # What we want is for - login user to own the buildout and the cache.  -
    # effective user to be own the var dir + able to read buildout and cache.

    api.env.hostout.runescalatable ("find %(path)s  -maxdepth 1 -mindepth 1 ! -name var -exec chown -R %(buildout)s:%(buildoutgroup)s '{}' \; " \
             " -exec chmod -R u+rw,g+r-w,o-rw '{}' \;" % locals())

    # command to set any +x file to also be +x for the group too. runzope and zopectl are examples
    if effective == buildout:
        with asbuildoutuser():
            api.run("find %(path)s -perm -u+x ! -path %(var)s -exec chmod g+x '{}' \;" % dict(path=path,var=var))
    else:
        api.sudo("find %(path)s -perm -u+x ! -path %(var)s -exec chmod g+x '{}' \;" % dict(path=path,var=var))


    api.env.hostout.runescalatable ('mkdir -p %(var)s' % locals())
#    api.run('mkdir -p %(var)s' % dict(var=var))

    if effective != buildout:
        try:
            api.sudo (\
                    '[ `stat -c %%U:%%G %(var)s` = "%(effective)s:%(buildoutgroup)s" ] || ' \
                    'chown -R %(effective)s:%(buildoutgroup)s %(var)s ' % locals())
            api.sudo ( '[ `stat -c %%A %(var)s` = "drwxrws--x" ] || chmod -R u+rw,g+wrs,o-rw %(var)s ' % locals())
        except:
            pass
            #raise Exception ("Was not able to set owner and permissions on "\
            #            "%(var)s to %(effective)s:%(buildoutgroup)s with u+rw,g+wrs,o-rw" % locals() )


#    api.sudo("chmod g+x `find %(path)s -perm -g-x` || find %(path)s -perm -g-x -exec chmod g+x '{}' \;" % locals()) #so effective can execute code
#    api.sudo("chmod g+s `find %(path)s -type d` || find %(path)s -type d -exec chmod g+s '{}' \;" % locals()) # so new files will keep same group
#    api.sudo("chmod g+s `find %(path)s -type d` || find %(path)s -type d -exec chmod g+s '{}' \;" % locals()) # so new files will keep same group

    api.env.hostout.runescalatable('mkdir -p %s %s/dist %s' % (bc, dl, ec))

    # the buildout dir should not be accessible to anyone but the buildout group
    api.sudo("chmod -R o-xrw %s"%path)
    # neither should the python files
    api.sudo("chmod -R o-xrw %s %s/dist %s"%(bc, dl, ec))
    api.sudo("chgrp %s %s %s/dist %s"%(buildoutgroup, bc, dl, ec))
    #find and change all the eggs so group can access them
    api.sudo("find %(bc)s -perm -u+rw ! -exec chmod g+r '{}' \;" % dict(bc=bc))



    #api.sudo('sudo -u $(effectiveuser) sh -c "export HOME=~$(effectiveuser) && cd $(install_dir) && bin/buildout -c $(hostout_file)"')

#    sudo('chmod 600 .installed.cfg')
#    sudo('find $(install_dir)  -type d -name var -exec chown -R $(effectiveuser) \{\} \;')
#    sudo('find $(install_dir)  -type d -name LC_MESSAGES -exec chown -R $(effectiveuser) \{\} \;')
#    sudo('find $(install_dir)  -name runzope -exec chown $(effectiveuser) \{\} \;')


def bootstrap_users():
    """ create users if needed """

    hostout = api.env.get('hostout')
    buildout = api.env['buildout-user']
    effective = api.env['effective-user']
    buildoutgroup = api.env['buildout-group']
    owner = buildout

#    from fabric.exceptions import NetworkError


    try:
        api.run ("egrep ^%(owner)s: /etc/passwd && egrep ^%(effective)s: /etc/passwd  && egrep ^%(buildoutgroup)s: /etc/group" % locals())

    except:
        try:
            api.sudo('groupadd %s || echo "group exists"' % buildoutgroup)
            addopt = " -M -g %s" % buildoutgroup
            addopt_noM = " -g %s" % buildoutgroup
            api.sudo('egrep ^%(owner)s: /etc/passwd || useradd %(addopt)s %(owner)s || useradd %(addopt_noM)s %(owner)s' % dict(owner=owner, addopt=addopt, addopt_noM=addopt_noM))
            api.sudo('egrep ^%(effective)s: /etc/passwd || useradd %(addopt)s %(effective)s || useradd %(addopt_noM)s %(effective)s' % dict(effective=effective, addopt=addopt, addopt_noM=addopt_noM))
            api.sudo('gpasswd -a %(owner)s %(buildoutgroup)s' % dict(owner=owner, buildoutgroup=buildoutgroup))
            api.sudo('gpasswd -a %(effective)s %(buildoutgroup)s' % dict(effective=effective, buildoutgroup=buildoutgroup))
        # except NetworkError:
        #     raise
        except:
            raise Exception (("Was not able to create users and groups." +
                    "Please set these group manualy." +
                    " Buildout User: %(buildout)s, Effective User: %(effective)s, Common Buildout Group: %(buildoutgroup)s")
                    % locals() )

    if not api.env.get("buildout-password",None):
        key_filename, key = api.env.hostout.getIdentityKey()
        try:
            #Copy authorized keys to buildout user:
            for owner in [api.env['buildout-user']]:
               copy_key(owner, key)

        except:
            raise Exception ("Was not able to create buildout-user ssh keys, please set buildout-password insted.")


def copy_key(owner, key):

        # if user is the same as the current user then no need to run
        # as sudo
        if owner == api.env["user"]:
            use_sudo = False
            run = api.run
        else:
            use_sudo = True
            run = api.sudo

        run("mkdir -p ~%s/.ssh" % owner)
        run('touch ~%s/.ssh/authorized_keys' % owner)
        fabric.contrib.files.append( text=key,
                filename='~%s/.ssh/authorized_keys' % owner,
                use_sudo=use_sudo )
        run("chown -R %(owner)s ~%(owner)s/.ssh" % locals() )
        run("chmod go-rwx ~%(owner)s/.ssh ~%(owner)s/.ssh/authorized_keys" % locals() )



def bootstrap_buildout():
    """ Create an initialised buildout directory """
    # bootstrap assumes that correct python is already installed


    # First ensure all needed directories are created and have right permissions
    path = api.env.path
    buildout = api.env['buildout-user']
    buildoutgroup = api.env['buildout-group']
    # create buildout dir

    if path[0] == "/":
        save_path = api.env.path # the pwd may not yet exist
        api.env.path = "/"

    api.env.hostout.runescalatable ('mkdir -p -m ug+x %(path)s' % dict(
        path=path,
        buildout=buildout,
        buildoutgroup=buildoutgroup,
    ))

    if path[0] == "/":
        api.env.path = save_path # restore the pwd

    api.env.hostout.requireOwnership (path, user=buildout, group=buildoutgroup, recursive=True)

    # ensure buildout user and group and cd in (ug+x)
    parts = path.split('/')
    for i in range(2, len(parts)):
        try:
            api.env.hostout.runescalatable('test -x %(p)s || chmod ug+x %(p)s' % dict(p='/'.join(parts[:i])) )
        except:
            print sys.stderr, "Warning: Not able to chmod ug+x on dir " + os.path.join(*parts[:i])


    buildoutcache = api.env['buildout-cache']
    api.env.hostout.runescalatable ('mkdir -p %s' % os.path.join(buildoutcache, "eggs"))
    api.env.hostout.runescalatable ('mkdir -p %s' % os.path.join(buildoutcache, "download/dist"))
    api.env.hostout.runescalatable ('mkdir -p %s' % os.path.join(buildoutcache, "downloads/extends"))

    api.env.hostout.requireOwnership (buildoutcache, user=buildout, recursive=True)


    #api.env.hostout.setowners()

#    api.run('mkdir -p %s/eggs' % buildoutcache)
#    api.run('mkdir -p %s/downloads/dist' % buildoutcache)
#    api.run('mkdir -p %s/extends' % buildoutcache)
    #api.run('chown -R %s:%s %s' % (buildout, buildoutgroup, buildoutcache))

    with asbuildoutuser():
        version = api.env['python-version']
        major = '.'.join(version.split('.')[:2])
        bootstrap = resource_filename(__name__, 'bootstrap.py')
        with cd(path):

            if not contrib.files.exists("bin/python") or major not in api.run("bin/python -V"):

                pythonbin = api.env.hostout.getpythonbin()
                python = "%s/python%s" % (pythonbin, major)
                if contrib.files.exists("%svirtualenv%s" % (pythonbin, major)):
                    api.run("%svirtualenv%s --no-setuptools ." % (pythonbin, major))
                elif contrib.files.exists("%seasy_install%s" % (pythonbin, major)):
                    # try to activate virtualenv if it exists
                    api.run("%(pythonpath)s/easy_install-%(major)s virtualenv"%dict(pythonpath=pythonbin,major=major))
                    api.run("%(pythonpath)s/virtualenv-%(major)s --no-setuptools ."%dict(pythonpath=pythonbin,major=major))



                # get setuptools first
                #if not contrib.files.exists("%s/easy_install" % pythonbin):
                #    get_url("https://bitbucket.org/pypa/setuptools/raw/bootstrap/ez_setup.py", output="/tmp/ez_setup.py")
                #    api.run("%s /tmp/ez_setup.py" % python)

                # get virtualenv first
                elif not contrib.files.exists("%spip" % pythonbin):
                    with cd("/tmp"):
                        #get_url("https://github.com/pypa/virtualenv/tarball/develop", output="virtualenv.tar.gz")
                        #api.run("tar xvfz virtualenv.tar.gz")
                        get_url("https://raw.githubusercontent.com/pypa/virtualenv/master/virtualenv.py", output="virtualenv.py")

                        #setup = api.run("find pypa-virtualenv* -name virtualenv.py ")
                        setup = "virtualenv.py"
                    api.run("%s /tmp/%s %s --no-setuptools --no-pip --no-wheel" % (python, setup, path))
                    #api.run("rm -r /tmp/pypa-virtualenv*")
                else:
                    api.run("%spip install virtualenv" % pythonbin)
                    #api.run("%s %s --no-setuptools %s" % (python, setup, path))
                    api.run("%s/virtualenv --no-setuptools ." % (pythonbin))

            # now we have bin/python

            # Some reason we need to ensure we have latest setup tools for latest bootstrap.py
            #api.run("bin/easy_install -u setuptools")
            #python += "source /var/buildout-python/python/python-%(major)s/bin/activate; python "

        with cd(path):
            api.put(bootstrap, '%s/bootstrap.py' % path)
            # put in simplest buildout to get bootstrap to run
            versions = api.env.hostout.getVersions()
            buildout_version = versions.get('zc.buildout','1.4.3')
            api.run('echo "[buildout]\n[versions]\nzc.buildout = %s" > buildout.cfg' % buildout_version)

            #python = "PATH=\$PATH:\"%s\"; %s" % (pythonpath, python)

            # Bootstrap baby!
            #try:
            #with fabric.context_managers.path(pythonpath,behavior='prepend'):
            api.run('%s bin/python bootstrap.py -v %s' % (proxy_cmd(), buildout_version) )
            #except:
            #    python = os.path.join (api.env["python-prefix"], "bin/", python)
            #    api.run('%s %s bootstrap.py --distribute' % (proxy_cmd(), python) )


def getpythonbin():
    version = api.env['python-version']
    major = '.'.join(version.split('.')[:2])

    pythonpath = os.path.join (api.env.get("python-path"),'bin')
    if contrib.files.exists(pythonpath):
       return pythonpath
    # see if we have the right python version installed
    pythonpath = api.run("python%s -c 'import sys; print sys.executable'" % major)
    pythonpath = pythonpath.rstrip("python%s" % major)
    return pythonpath




#def bootstrap_buildout_ubuntu():
#
##    api.sudo('apt-get update')
#
#    api.sudo('apt-get -yq install '
#             'build-essential ')
#
#    api.sudo('apt-get -yq install '
#             'python-dev ')
#
#    api.env.hostout.bootstrap_buildout()

def bootstrap_python_buildout():
    "Install python from source via buildout"

    #TODO: need a better way to install from source that doesn't need svn or python
    # we need libssl-dev, python and other buildtools

    path = api.env.path

    BUILDOUT = """
[buildout]
extends =
      src/base.cfg
      src/readline.cfg
      src/libjpeg.cfg
      src/python%(majorshort)s.cfg
      src/links.cfg

parts =
      ${buildout:base-parts}
      ${buildout:readline-parts}
      ${buildout:libjpeg-parts}
      ${buildout:python%(majorshort)s-parts}
      ${buildout:links-parts}

python-buildout-root = ${buildout:directory}/src

# ucs4 is needed as lots of eggs like lxml are also compiled with ucs4 since most linux distros compile with this
[python-%(major)s-build:default]
extra_options +=
    --enable-unicode=ucs4
    --with-threads
    --with-readline
    --with-dbm
    --with-zlib
    --with-ssl
    --with-bz2
patch = %(patch_file)s

[install-links]
prefix = ${buildout:directory}


"""

    patch = r"""
--- Modules/Setup.dist	2005-12-28 04:37:16.000000000 +1100
+++ Modules/Setup.dist	2012-05-23 23:31:22.000000000 +1000
@@ -198,14 +198,14 @@
 #_csv _csv.c

 # Socket module helper for socket(2)
-#_socket socketmodule.c
+_socket socketmodule.c

 # Socket module helper for SSL support; you must comment out the other
 # socket line above, and possibly edit the SSL variable:
-#SSL=/usr/local/ssl
-#_ssl _ssl.c \\
-#	-DUSE_SSL -I$(SSL)/include -I$(SSL)/include/openssl \\
-#	-L$(SSL)/lib -lssl -lcrypto
+SSL=/usr/lib/ssl
+_ssl _ssl.c \\
+	-DUSE_SSL -I/usr/include/openssl \\
+	-L/usr/lib/ssl -lssl -lcrypto

 # The crypt module is now disabled by default because it breaks builds
 # on many systems (where -lcrypt is needed), e.g. Linux (I believe).
"""

    hostout = api.env.hostout
    hostout = api.env.get('hostout')
    sudouser = api.env.get('user')
    buildout = api.env['buildout-user']
    effective = api.env['effective-user']
    buildoutgroup = api.env['buildout-group']

    #hostout.setupusers()
#    api.sudo('mkdir -p %(path)s' % locals())
#    hostout.setowners()

    version = api.env['python-version']
    major = '.'.join(version.split('.')[:2])
    majorshort = major.replace('.','')

    prefix = api.env["python-path"]
    if not prefix:
        raise "No path for python set"
    save_path = api.env.path # the pwd may not yet exist
    api.env.path = "/"
    with cd('/'):
        if buildout != sudouser:
            sudo('mkdir -p %s' % prefix)
            sudo('chown %s:%s %s'%(buildout,buildoutgroup,prefix))
        else:
            run('mkdir -p %s' % prefix)
            run('chown %s:%s %s'%(buildout,buildoutgroup,prefix))



    with asbuildoutuser():
      #TODO: bug in fabric. seems like we need to run this command first before cd will work
      hostos = api.env.hostout.detecthostos().lower()
      with cd(prefix):
        get_url("http://github.com/collective/buildout.python/tarball/master", output="collective-buildout.python.tar.gz")
        api.run('tar --strip-components=1 -zxvf collective-buildout.python.tar.gz')

        #api.sudo('svn co http://svn.plone.org/svn/collective/buildout/python/')
        #get_url('http://python-distribute.org/distribute_setup.py',  api.sudo)
        #api.run('%s python distribute_setup.py'% proxy_cmd())
        # -v due to https://github.com/collective/buildout.python/issues/11

        if hostos == 'ubuntu' and major=='2.4':
            patch_file = '${buildout:directory}/ubuntussl.patch'
            api.run('rm -f ubuntussl.patch')
            fabric.contrib.files.append('ubuntussl.patch', patch, use_sudo=False,escape=True)
        else:
            patch_file = ''

        api.run('rm buildout.cfg')
        fabric.contrib.files.append('buildout.cfg', BUILDOUT%locals(), use_sudo=False)
        # Overwrite with latest bootstrap
        #get_url('http://svn.zope.org/*checkout*/zc.buildout/trunk/bootstrap/bootstrap.py')

        #create a virtualenv to run collective.buildout in
        # upgrade from 1.7 to 1.10.1, pin down a version to get a stable version
#        get_url('https://raw.github.com/pypa/virtualenv/1.10.1/virtualenv.py')
#        api.run("%s python virtualenv.py --no-site-packages buildoutenv"  % proxy_cmd())

        with cd("/tmp"):
            get_url("https://github.com/pypa/virtualenv/tarball/develop", output="virtualenv.tar.gz")
            api.run("tar xvfz virtualenv.tar.gz")
            setup = api.run("find pypa-virtualenv* -name virtualenv.py ")
        api.run("python /tmp/%s  buildoutenv" % ( setup))
        api.run("rm -r /tmp/pypa-virtualenv*")
        api.run("%s buildoutenv/bin/pip install setuptools==1.4.2"  % proxy_cmd())


        api.run('source buildoutenv/bin/activate')
        # old version is 'python -S bootstrap.py', but it does not work and got error
        # no module on shutil, so '-S' is removed.

#    dockerfile.run_all("""python -c "import urllib; urllib.urlretrieve('%s', '%s')" """ %
#                       ("https://bootstrap.pypa.io/bootstrap-buildout.py","bootstrap-buildout.py"))
#    dockerfile.run_all('chown -R plone.plone . && chmod -R a+rwx .')
#    dockerfile.run_all('cd %(path)s && virtualenv . && '
#                      'bin/pip install setuptools==%(sv)s && '
#                       'echo "[buildout]" > buildout.cfg && '
##                       'echo "[buildout]\n[versions]\nzc.buildout = %s" > buildout.cfg' % buildout_version)
#                       'bin/python bootstrap-buildout.py --buildout-version=%(bv)s -c %(bf)s --setuptools-version=%(sv)s'
#                            % dict(path=path, bv=buildout_version, bf="buildout.cfg", sv=setuptools_version))


        api.run('%s source buildoutenv/bin/activate; python bootstrap.py' % proxy_cmd())
        api.run('%s source buildoutenv/bin/activate; bin/buildout -N'%proxy_cmd())
        #api.env['python'] = "source /var/buildout-python/python/python-%(major)s/bin/activate; python "
        #api.run('%s bin/install-links'%proxy_cmd())
        api.run(" source buildoutenv/bin/activate; bin/virtualenv-%(major)s ."%dict(major=major))
        #api.env['python-path'] = "/var/buildout-python/python-%(major)s" %dict(major=major)
        api.env["system-python-use-not"] = True
        #api.run('%s %s/bin/python distribute_setup.py' % (proxy_cmd(), api.env['python-path']) )


    #ensure bootstrap files have correct owners
    #hostout.setowners()

def bootstrap_python(extra_args=""):
    version = api.env['python-version']

    versionParsed = '.'.join(version.split('.')[:3])

    d = dict(version=versionParsed)

    prefix = api.env["python-path"]
    if not prefix:
        raise "No path for python set"
    save_path = api.env.path # the pwd may not yet exist
    api.env.path = "/"
    with cd('/'):
        runescalatable('mkdir -p %s' % prefix)
    #api.run("([-O %s])"%prefix)

    with asbuildoutuser():
      with cd('/tmp'):
        get_url('http://python.org/ftp/python/%(version)s/Python-%(version)s.tgz'%d)
        api.run('tar -xzf Python-%(version)s.tgz'%d)
        with cd('Python-%(version)s'%d):
#            api.run("sed 's/#readline/readline/' Modules/Setup.dist > TMPFILE && mv TMPFILE Modules/Setup.dist")
#            api.run("sed 's/#_socket/_socket/' Modules/Setup.dist > TMPFILE && mv TMPFILE Modules/Setup.dist")

            api.run('./configure --prefix=%(prefix)s  --enable-unicode=ucs4 --with-threads --with-readline --with-dbm --with-zlib --with-ssl --with-bz2 %(extra_args)s' % locals())
            api.run('make')
            runescalatable('make altinstall')
        api.run("rm -rf /tmp/Python-%(version)s"%d)
    api.env["system-python-use-not"] = True
    api.env.path = save_path



def bootstrap_python_ubuntu():
    """Update ubuntu with build tools, python and bootstrap buildout"""
    hostout = api.env.get('hostout')
    path = api.env.path


    version = api.env['python-version']
    major = '.'.join(version.split('.')[:2])


    try:
        api.sudo('apt-get -yq install software-properties-common python-software-properties')
        api.sudo('add-apt-repository -yq ppa:fkrull/deadsnakes')
    except:
        #version of ubunut too early
        pass

    api.sudo('apt-get update')

    #Install and Update Dependencies


    #contrib.files.append(apt_source, '/etc/apt/source.list', use_sudo=True)
    api.sudo('apt-get -yq update ')
    api.sudo('apt-get -yq install '
             'build-essential '
#             'python-libxml2 '
#             'python-elementtree '
#             'python-celementtree '
             'ncurses-dev '
             'libncurses5-dev '
# needed for lxml on lucid
             'libz-dev '
             'libbz2-dev '
             'libxp-dev '
#             'libssl-dev '
             'curl '
#             'openssl '
#             'python-openssl '
             )
    try:
        api.sudo('apt-get -yq install libreadline5-dev ')
    except:
        api.sudo('apt-get -yq install libreadline-gplv2-dev ')

    #api.sudo('apt-get -yq build-dep python%(major)s '%dict(major=major))

    api.sudo('apt-get -yq install python%(major)s python%(major)s-dev '%locals())

    #api.sudo('apt-get -yq update; apt-get dist-upgrade')

#    api.sudo('apt-get install python2.4=2.4.6-1ubuntu3.2.9.10.1 python2.4-dbg=2.4.6-1ubuntu3.2.9.10.1 \
# python2.4-dev=2.4.6-1ubuntu3.2.9.10.1 python2.4-doc=2.4.6-1ubuntu3.2.9.10.1 \
# python2.4-minimal=2.4.6-1ubuntu3.2.9.10.1')
    #wget http://mirror.aarnet.edu.au/pub/ubuntu/archive/pool/main/p/python2.4/python2.4-minimal_2.4.6-1ubuntu3.2.9.10.1_i386.deb -O python2.4-minimal.deb
    #wget http://mirror.aarnet.edu.au/pub/ubuntu/archive/pool/main/p/python2.4/python2.4_2.4.6-1ubuntu3.2.9.10.1_i386.deb -O python2.4.deb
    #wget http://mirror.aarnet.edu.au/pub/ubuntu/archive/pool/main/p/python2.4/python2.4-dev_2.4.6-1ubuntu3.2.9.10.1_i386.deb -O python2.4-dev.deb
    #sudo dpkg -i python2.4-minimal.deb python2.4.deb python2.4-dev.deb
    #rm python2.4-minimal.deb python2.4.deb python2.4-dev.deb

    # python-profiler?

def bootstrap_python_redhat():
    hostout = api.env.get('hostout')
    #Install and Update Dependencies
    user = hostout.options['user']

    # When a python is needed to be installed
    def python_build():
        # Install packages to build
        required = [
 #               "libxml2-devel",
                "ncurses-devel",
                "zlib",
                "zlib-devel",
                "readline-devel",
                "bzip2-devel",
#                "openssl",
#                "openssl-dev"
        ]
        try:
            api.sudo ('yum -y install' + ' '.join(required))
        except:

            # Can't install - test to see if the packages exist
            notInstalled = []
            for pkg in required:
                try:
                    api.run ('rpm -aq | grep %(pkg)s' % locals())
                except:
                    notInstalled.append(pkg)

            # Packages not found! Raise Exception
            if len(notInstalled):
                raise Exception (
                        "Could not determin if required pacakges were installed: "
                        + ' '.join(notInstalled))
        hostout.bootstrap_python_buildout()



    # Try to enable sudo access
    try:
        hostout.bootstrap_allowsudo()
    except:
        pass


    # RedHat pacakge management install

    # Redhat/centos don't have Python 2.6 or 2.7 in stock yum repos, use
    # EPEL.  Could also use RPMforge repo:
    # http://dag.wieers.com/rpm/FAQ.php#B
    #api.sudo("rpm -Uvh --force http://download.fedora.redhat.com/pub/epel/5/i386/epel-release-5-4.noarch.rpm")

    # for centos 6.0
    api.sudo('rpm -Uvh --force http://rpms.famillecollet.com/enterprise/remi-release-6.rpm http://dl.fedoraproject.org/pub/epel/6/`arch`/epel-release-6-8.noarch.rpm')


    version = api.env['python-version']
    python_versioned = 'python' + ''.join(version.split('.')[:2])

    try:
        api.sudo('yum -y install gcc gcc-c++ ')

        api.sudo('yum -y install ' +
                 python_versioned + ' ' +
                 python_versioned + '-devel ' +
                 'python-devel ' +
                 'python-setuptools '
                 'ncurses-devel '
                 'zlib zlib-devel '
                 'readline-devel '
                 'bzip2-devel '
                 'patch '
                 )
    except:
        # Couldn't install from rpm - failover build
        python_build()

#optional stuff
#    api.sudo('yum -y install ' +
#             'python-imaging '
#             'libjpeg-devel '
#             'freetype-devel '
#             'lynx '
#             'openssl-devel '
#             'libjpeg-devel '
#            'openssl openssl-devel '
#            'libjpeg libjpeg-devel '
#            'libpng libpng-devel '
#            'libxml2 libxml2-devel '
#            'libxslt libxslt-devel ')


def bootstrap_python_slackware():
    urls = [
        'http://carroll.cac.psu.edu/pub/linux/distributions/slackware/slackware-11.0/slackware/l/zlib-1.2.3-i486-1.tgz'
        ]
    for url in urls:
        with cd('/tmp'):
            get_url(url)
            pkg = url.rsplit('/',1)[-1]
            api.sudo('installpkg %s'%pkg)
            api.run("rm %s"%pkg)
    api.env.hostout.bootstrap_python(extra_args="--with-zlib=/usr/include/zlib.h")


def detecthostos():
    #http://wiki.linuxquestions.org/wiki/Find_out_which_linux_distribution_a_system_belongs_to
    # extra ; because of how fabric uses bash now
    if api.env.get('hostos',None):
        return api.env['hostos']
    hostos = api.run(
        "[ -e /etc/SuSE-release ] && echo SuSE || "
                "[ -e /etc/redhat-release ] && echo redhat || "
                "[ -e /etc/fedora-release ] && echo fedora || "
                "lsb_release -is || "
                "[ -e /etc/slackware-version ] && echo slackware"
               )
    if hostos:
        hostos = hostos.lower().strip().split()[0]
    print "Detected Hostos = %s" % hostos
    api.env['hostos'] = hostos
    return hostos


def bootstrap_allowsudo():
    """Allow any sudo without tty"""
    hostout = api.env.get('hostout')
    user = hostout.options['user']

    try:
        api.sudo("egrep \"^\%odas\ \ ALL\=\(ALL\)\ ALL\" \"/etc/sudoers\"",pty=True)
    except:
        api.sudo("echo '%odas  ALL=(ALL) ALL' >> /etc/sudoers",pty=True)

    try:
        api.sudo("egrep \"^Defaults\:\%%%(user)s\ \!requiretty\" \"/etc/sudoers\"" % dict(user=user), pty=True)
    except:
        api.sudo("echo 'Defaults:%%%(user)s !requiretty' >> /etc/sudoers" % dict(user=user), pty=True)




#def initcommand(cmd):
#    if cmd in ['uploadeggs','uploadbuildout','buildout','run']:
#        api.env.user = api.env.hostout.options['buildout-user']
#    else:
#        api.env.user = api.env.hostout.options['user']
#    key_filename = api.env.get('identity-file')
#    if key_filename and os.path.exists(key_filename):
#        api.env.key_filename = key_filename




def install_bootscript (startcmd, stopcmd, prefname=""):
    """Installs a system bootscript"""
    hostout = api.env.hostout

    buildout = hostout.getRemoteBuildoutPath()
    name = "buildout-" + (prefname or hostout.name)

    script = """
#!/bin/sh
#
# Supervisor init script.
#
# chkconfig: 2345 80 20
# description: supervisord

# Source function library.
#. /etc/rc.d/init.d/functions

ENV=plonedev
NAME="%(name)s"
BUILDOUT=%(buildout)s
RETVAL=0

start() {
    echo -n "Starting $NAME: "
    cd $BUILDOUT
    %(startcmd)s
    RETVAL=$?
    echo
    return $RETVAL
}

stop() {
    echo -n "Stopping $NAME: "
    cd $BUILDOUT
    %(stopcmd)s
    RETVAL=$?
    echo
    return $RETVAL
}

case "$1" in
	 start)
	     start
	     ;;

	 stop)
	     stop
	     ;;

	 restart)
	     stop
	     start
	     ;;
esac

exit $REVAL
    """ % locals()

    path = os.path.join("/etc/init.d", name)

#    tmpfile, tmpname = tempfile.mkstemp()
#    tmpfile.write(script)
#    tmpfile.close()
#    api.put(tmpname, '/tmp/%s'%name)
#    api.sudo("mv /tmp/%s %s" %(name, path))

    # Create script destroying one if it already exists
    api.sudo ("test -f '%(path)s' && rm '%(path)s' || echo 'pass'" % locals())
    contrib.files.append(
        text=script,
        filename=path,
        use_sudo=True )
    api.sudo ("chown root '%(path)s'" % locals())
    api.sudo ("chmod +x '%(path)s'" % locals())


    # Install script into system rc dirs
    api.sudo (  ("(which update-rc.d && update-rc.d '%(name)s' defaults) || "
                "(test -f /sbin/chkconfig && /sbin/chkconfig --add '%(name)s')") % locals() )


def uninstall_bootscript (prefname=""):
    """Uninstalls a system bootscript"""
    hostout = api.env.hostout
    name = "buildout-" + (prefname or hostout.name)
    path = os.path.join("/etc/init.d", name)
    api.sudo ((";(which update-rc.d && update-rc.d -f '%(name)s' remove) || "
              "(test -f /sbin/chkconfig && (/sbin/chkconfig --del '%(name)s' || echo 'pass' ))") % locals())
    api.sudo ("test -f '%(path)s' && rm '%(path)s' || echo 'pass'" % locals())


def bootscript_list():
    """Lists the buildout bootscripts that are currently installed on the host"""
    api.run ("ls -l /etc/init.d/buildout-*")


def proxy_cmd():
    if api.env.hostout.http_proxy:
        return 'export HTTP_PROXY="http://%s" && '% api.env.hostout.http_proxy
    else:
        return ''

def get_url(curl, cmd=api.run, output=None):
    proxy = api.env.hostout.socks_proxy
    if False and output:
        test = "test -f %s ||"%output
    else:
        test = ""

    if proxy:
        cmd('%s curl -L --socks5 %s %s %s' % (test, proxy, '-o %s'%output if output else '-O', curl) )
    else:
        #cmd('%s curl -L %s %s' % (test, '-o %s'%output if output else '-O', curl))
        if not output:
            output = curl.split('/')[-1]
        cmd("""python -c "import urllib; urllib.urlretrieve('%s', '%s')" """ % (curl,output))


#        api.run('test -f collective-buildout.python.tar.gz || wget http://github.com/collective/buildout.python/tarball/master -O collective-buildout.python.tar.gz --no-check-certificate')

try:
    from dockermap.api import DockerClientWrapper, DockerFile
    from docker.utils import kwargs_from_env

    class myclient(DockerClientWrapper):
        def push_progress(self, status, object_id, progress):
            print progress
        def push_log(self, info, level):
            print info

except:
    from dockermap.build.dockerfile import DockerFile


def _basedockerfile(dockerfile):
    # TODO: need a way to install python on any platform
    # TODO: Need a way to get rid of buildtools after running buildout
    hostimage = api.env.get('hostimage', 'alpine')


    hostout = api.env.hostout
    versions = hostout.getVersions()
    buildout_version = versions.get('zc.buildout','1.4.3')
    setuptools_version = versions.get('setuptools','6.0.2')
    path = api.env.path


    params = dict(user=api.env['buildout-user'],
                  group=api.env['buildout-group'],
                  effective=api.env['effective-user'],
                  path=path,
                  bv=buildout_version,
                  bf="buildout.cfg",
                  sv=setuptools_version,
                  bootstrap_url="https://bootstrap.pypa.io/bootstrap-buildout.py",
                  bootstrap="bootstrap-buildout.py")

    dockerfile.prefix('USER', 'root')
    if 'python' in hostimage:
        dockerfile.run_all("pip install virtualenvwrapper")
        dockerfile.run_all(('adduser --system --disabled-password --shell /bin/bash '
                           '--group --home /home/{user} --gecos "{user} system user" -u 1000 {user}').format(**params))
    elif 'ubuntu' in hostimage or 'debian' in hostimage:
        dockerfile.run_all("rm -rf /var/lib/apt/lists/* &&"
                           "apt-get update &&"
                           "apt-get upgrade -y -q &&"
                           "apt-get dist-upgrade -y -q &&"
                           "apt-get -y -q autoclean &&"
                           "apt-get -y -q autoremove && "
                           "apt-get install -y -q --fix-missing python-dev python-pip libssl-dev && "
                           "pip install virtualenvwrapper")
        dockerfile.run_all('adduser --system --disabled-password --shell /bin/bash '
                           '--group --home /home/{user} --gecos "{user} system user" -u 1000 {user}'.format(**params))
    elif 'alpine' in hostimage:
        dockerfile.run_all("apk --no-cache add python python-dev build-base py-pip ca-certificates && "
                           "update-ca-certificates && "
                           "pip install virtualenvwrapper")
        dockerfile.run_all('addgroup -g 1000 {group} && adduser -S -D -s /bin/bash -G {group} '
                           ' -h /home/{user} -g "{user} system user" -u 1000 {user}'.format(**params))
    else:
        dockerfile.run_all("pip install virtualenvwrapper")


    #upload the eggs
    dl = hostout.getDownloadCache()
    buildoutcache = api.env['buildout-cache']
    cmds = []
    buildout_dirs = '%s %s %s %s' % (os.path.join(buildoutcache),
                                  os.path.join(buildoutcache, "eggs"),
                                  os.path.join(dl, "dist"),
                                  os.path.join(dl, "extends"))
    cmds += ['mkdir -p %s' % buildout_dirs]
    cmds += ['chown -R {user}:{group} %s'.format(**params) % buildout_dirs]
    cmds += ['mkdir -p {path}'.format(**params)]
    cmds += ['chown -R {user}:{group} {path}'.format(**params)]
    dockerfile.run_all(' && '.join(cmds))

    dockerfile.run_all(' && '.join(hostout.getPreCommands()))
#    bootstrap = resource_filename(__name__, 'bootstrap.py')
#    dockerfile.add_file(bootstrap, 'bootstrap.py')
    dockerfile.prefix('USER', params['user'])
    cmds = []
    cmds += ['cd {path} && test ! -e "bin/buildout"'.format(**params)]
    cmds += ['chown -R {user}.{group} . && chmod -R a+rwx .'.format(**params)]
    cmds += ["""(python -c 'import urllib; urllib.urlretrieve("{bootstrap_url}", "{bootstrap}")' ) """.format(**params)]
    cmds += ['virtualenv . ']
    cmds += ['mkdir -p {path}/var/tmp '.format(**params)]
    cmds += ['bin/pip install setuptools=={sv} && '
             'echo "[buildout]" > buildout.cfg && '
#            'echo "[buildout]\n[versions]\nzc.buildout = %s" > buildout.cfg' % buildout_version)
             'bin/python bootstrap-buildout.py --buildout-version={bv} '
             '-c buildout.cfg --setuptools-version={sv} '
             '|| echo "Buildout exists"'.format(**params)]
    dockerfile.run_all(' && '.join(cmds))
    dockerfile.prefix('ENV', 'TMPDIR %s/var/tmp' % path)
    dockerfile.prefix('WORKDIR', path)

    #HACK
    #dockerfile.run_all('apt-get install python-docutils')
    dockerfile.prefix('USER', 'root')
    dockerfile.run_all('chown -R {user}.{group} {path} . && chmod -R a+rwx .'.format(**params))
    dockerfile.prefix('USER', params['effective'])


def _buildoutdockerfile(dockerfile, bundle_file, buildout_filename):
    hostout = api.env.hostout
    path = api.env.path
    dl = hostout.getDownloadCache()

    def reset(tarinfo):
        tarinfo.uid = tarinfo.gid = 0
        tarinfo.uname = tarinfo.gname = api.env['buildout-group']
        return tarinfo

    bundle = tarfile.open(bundle_file, 'w:')
    for pkg in hostout.localEggs():
        name = os.path.basename(pkg)
        #dockerfile.add_file(pkg, os.path.join(dl, 'dist', name))
        bundle.add(pkg, os.path.join(dl, 'dist', name), filter=reset)

    # move our buildout files over
    for fileabs, filerel in hostout.getHostoutPackageFiles():
        bundle.add(fileabs, os.path.join(path, filerel), filter=reset )

    # Now upload pinned.cfg.
    pinned = "[buildout]\ndevelop=\nauto-checkout=\n" + \
        "find-links += "+ os.path.join(dl, 'dist') + '\n' \
        "[versions]\n"+hostout.packages.developVersions()
    pinnedtmp = open('parts/pinned.cfg',"w")
    pinnedtmp.write(pinned)
    pinnedtmp.close()
    bundle.add(pinnedtmp.name, os.path.join(path, 'pinned.cfg'), filter=reset)


    #upload generated cfg with hostout versions
    hostout_file=hostout.getHostoutFile()
    try:
        overwrite = open('parts/'+buildout_filename, "r").read() != hostout_file
    except IOError:
        overwrite = True

    if overwrite:
        hostout_tmp = open('parts/'+buildout_filename, "w")
        hostout_tmp.write(hostout_file)
        hostout_tmp.close()
    bundle.add('parts/'+buildout_filename, os.path.join(path, buildout_filename), filter=reset)

    bundle.close()
    dockerfile.add_file(bundle_file, '/', bundle_file)

def _startupdockerfile(dockerfile, buildout_filename):
    hostout = api.env.hostout
    path = api.env.path

    params = dict(user=api.env['buildout-user'],
                  group=api.env['buildout-group'],
                  effective=api.env['effective-user'],
                  path=path)

    dockerfile.prefix('WORKDIR', path)
#    dockerfile.expose = "22 80 8101"
    dockerfile.prefix('USER', params['user'])
    # on run we do one quick offline build so we can include env vars
    commands = ['cd %s' % path,
#                'chown -R {user}:{group} var'.format(**params),
#                'chmod -R a+rwx var',
                './bin/buildout -NOc %s' % buildout_filename,
    ]
    commands += hostout.getPostCommands()
    command = ' && '.join(commands)
    dockerfile.entrypoint = '/bin/sh -c'
    dockerfile.command=['%s' % (command)]
    #TODO remove any entry point

def dockerfile(path=None):
    hostout = api.env.hostout
    if path is None:
        path = hostout.name
    if not os.path.exists(path):
        os.makedirs(path)
    ## needs to be done relative to the base dir
    #hostout.getHostoutFile()
    #old_path = os.getcwd()
    #os.chdir(path)

    hostimage = api.env.get('hostimage', 'alpine')
    dockerfile = DockerFile(hostimage, maintainer='ME, me@example.com')
    _basedockerfile(dockerfile)

    buildout_filename = "hostout-gen-%s.cfg" % hostout.name
    _buildoutdockerfile(dockerfile, 'buildout_bundle.tar', buildout_filename)

    # Make sure user has ownership of all files
    dockerfile.prefix('USER', 'root')
    dockerfile.run_all('chown -R %s.%s %s' % (api.env['buildout-user'],
                                              api.env['buildout-group'],
                                              api.env.path))
    dockerfile.prefix('USER', api.env['effective-user'])

    # To help with caching we will run install each buildout part as a seperate command
    # Just use parts = with multiple parts
    # TODO turn off with command line switch
    for i in range(1, len(hostout.parts)):
        baseparts = ' '.join(hostout.parts[0:i])
        dockerfile.run_all('./bin/buildout -Nc %s install %s' % (buildout_filename, baseparts))

    dockerfile.run_all('./bin/buildout -Nc %s' % (buildout_filename))
    _startupdockerfile(dockerfile, buildout_filename)
    dockerfile.finalize()
    with open(os.path.join(path, 'Dockerfile'), "w") as dfile:
        dfile.write(dockerfile.getvalue())
    shutil.copyfile('buildout_bundle.tar', os.path.join(path, 'buildout_bundle.tar'))
    #os.chdir(old_path)


def dockerbuild():
    """ If we use a straight dockerfile approach then any change in the buildout or failure during buildout
        means we have to start again.
        Instead we will
        1. build a base image for buildout using dockerfile
        2. create a image with all our custom buildout files in using dockerfile
        3. run the buildout and save the image even if the buildout fails
        4. if failed then repeat 2 but use the failed image as the base image
        5. if passed then flatten the image and tag
    """
    hostout = api.env.hostout
    path = api.env.path
    client = myclient(**kwargs_from_env(assert_hostname=False))
    hostimage = api.env.get('hostimage', 'alpine')
    params = dict(user=api.env['buildout-user'],
                  group=api.env['buildout-group'],
                  effective=api.env['effective-user'],
                  path=path)

    dockerfile = DockerFile(hostimage, maintainer='ME, me@example.com')
    _basedockerfile(dockerfile)

    image = client.build_from_file(dockerfile, tag='hostout/%s:base' % hostout.name, rm=True)
    if not image:
        return
    #image = hostimage

    def is_baseimage_of(base, imagename):
        #import pdb; pdb.set_trace()
        for i in client.images():
            if imagename not in i['RepoTags']:
                continue
            image = i['Id']
            for history_image in client.history(image):
                if history_image.get('Id','')[:len(base)] == base:
                    return True
        return False


    # retry where we left off
    # HACK. What if the buildoutbase changed?
    if is_baseimage_of(image, "hostout/%s:latest" % hostout.name):
        baseimage = "hostout/%s:latest" % hostout.name
        print "uploading updated local buildout into previous failed image of %s" % baseimage
    else:
        baseimage = image
        print "building new image on top of %s" % baseimage

    dockerfile = DockerFile(baseimage, maintainer='ME, me@example.com')
    buildout_filename = "hostout-gen-%s.cfg" % hostout.name
    _, bundle_file = tempfile.mkstemp(suffix='.tar')

    _buildoutdockerfile(dockerfile, bundle_file, buildout_filename)

    image = client.build_from_file(dockerfile, tag='hostout/%s:latest' % hostout.name, rm=True, stream=True)

    if not image:
        return False

    # We run our build outside of dockerfile so we can resume the build if it fails
    volumes = "%s:%s" % (hostout.packages.download_cache, hostout.getDownloadCache())
#    print "mapping local folders: %s" % volumes

    container = client.create_container(image,
                                        working_dir=path,
                                        entrypoint='./bin/buildout -Nc %s' % (buildout_filename),
 #                                       volumes=[volumes]
                                      )
    client.start(container)
    error = 0
    for line in client.logs(container, stderr=True, stream=True):
        print line,
        if line.startswith('While: '):
            error = 1
        if error==1 and line.startswith('Error: '):
            error = 2

    image = client.commit(container.get('Id'), repository="hostout/%s" % hostout.name, tag="latest")
    print "Saving image to %s." % "hostout/%s:latest" % hostout.name
    print "to build fresh: docker rmi %s" % image[u'Id']
    if error == 2:
        return

    # if it succeeded finish it off.
    dockerfile = DockerFile("hostout/%s:latest" % hostout.name, maintainer='ME, me@example.com')
    _startupdockerfile(dockerfile, buildout_filename)
    image = client.build_from_file(dockerfile, tag="hostout/%s:latest" % hostout.name, rm=True)
    print "Final image created %s" % image

def hotfix(url,dest='products'):
    """ Takes a url and will deploy that to your products directory. Don't forget to restart after """
    with api.cd('%s/%s'%(api.env.path, dest)):
        with asbuildoutuser():
            #api.run("curl %s /tmp/hotfix.zip"%url)
            #api.run("python -c \"import urllib; f=open('/tmp/hotfix.zip','w'); f.write(urllib.urlopen('%s').read()); f.close()\""%url)
            filename = os.path.basename(url)
            tmp = '/tmp/%s'%filename
            if not os.path.exists(tmp):
                f=open(tmp,'w')
                f.write(urllib.urlopen(url).read())
                f.close()
            api.put(tmp, tmp)
            try:
                api.run("unzip -o %s"%tmp)
            except:
                api.run("""python -c "import zipfile;import urllib;import StringIO; zipfile.ZipFile(StringIO.StringIO(urllib.urlopen('%s').read())).extractall()" """%url)

            group = api.env['buildout-group']
            api.run("chgrp -R %s ."%(group))
            api.run('rm %s'%tmp)
