Hostout - standardised deployment of buildout_ based applications with Fabric_
==============================================================================

Hostout gives you:

- the ability to configure your Fabric_ commands from within buildout_
- a framework for integrating different Fabric_ scripts via setup tools packages
- an out of the box deployment command for buildout_ based applications
- plugins to integrate deployment further such as  hostout.supervisor_ and
  hostout.cloud_


.. contents::


Overview
********

Hostout is a framework for managing remote buildouts via fabric scripts. It
includes many helpful built-in commands to package, deploy and bootstrap a
remote server based on your local buildout.

Hostout is built around two ideas:

1. Sharing your deployment configuration for an application in the same
   buildout_ you share with your developers in a team, so where and how your
   applications is deployed is automated rather than documentation.
   Deployment then becomes a simple command by any member of the team.

2. Sharing fabric scripts via PyPI so we don't have to reinvent ways
   to deploy or manage hosted applications.

If you are already a user of Fabric and buildout but aren't interested in
the hostout's built-in ability to deploy, then skip ahead to
`Integrating Fabric into buildout`_.

You don't need to learn Fabric_ to use hostout but you will need to learn
buildout_.  The good news is that many buildouts and snippets already exist
for building django, pylons, pyramid, plone, zope, varnish, apache, haproxy
or whichever server-side technology you want to deploy.


To Contribute
*************

To contribute:

- Code repository: http://github.com/collective/collective.hostout
- Questions and comments to http://github.com/collective/collective.hostout/issues
- Report bugs at http://github.com/collective/collective.hostout/issues


Hostout deploy
**************

Hostout deploy is a built-in Fabric command that packages your buildout and
any development eggs you might have, copies them to the server, prepares
the server to run and then runs buildout remotely for you. This makes it
simple to deploy your application.

Development buildout
--------------------

For example, let's say we had the worlds simplest wsgi application.
You can use ``paster`` to create the package. Go to ``src`` and type::

    ../bin/paster create -t basic_package hellowsgi version=0.1
        description="testing hostout" long_description="" keywords=""
        author="" author_email="" url="" license_name="" zip_safe=False

Then edit ``src/hellowsgi/hellowsgi/__init__.py`` as follow::

    from webob import Request, Response

    def main(global_config, **local_conf):
        return MainApplication()

    class MainApplication(object):
        """An endpoint"""

        def __call__(self, environ, start_response):
            request = Request(environ)
            response = Response("Powered by collective.hostout!")
            return response(environ, start_response)

Then edit ``src/hellowsgi/setup.py`` and update entry_points as follow::
    
    [...]
    entry_points="""
          # -*- Entry points: -*-
          [paste.app_factory]
          main = hellowsgi:main
          """,
    [...]

We will create a buildout configuration file called ``base.cfg`` ::

    [buildout]
    parts = demo pasterini
    develop =
      src/hellowsgi

    [demo]
    recipe=zc.recipe.egg
    eggs =
        PasteScript
        webob
        hellowsgi

    [pasterini]
    recipe = collective.recipe.template
    output = parts/demo/paster.ini
    port = 8080
    input = inline:
        [server:main]
        use = egg:Paste#http
        host = 0.0.0.0
        port = ${:port}

        [pipeline:main]
        pipeline = app

        [app:app]
        use = egg:hellowsgi#main

Once we bootstrap and build this::

    $ python bootstrap.py -c base.cfg
    $ bin/buildout -c base.cfg

we have a working wsgi app if you run ::

    $ bin/paster serve parts/demo/paster.ini

Production buildout
-------------------

Next you will create a "production buildout" which extends your
``base.cfg``.  This might contain parts to install webservers, databases,
caching servers etc.

Our ``prod.cfg`` is very simple ::

    [buildout]
    extends = base.cfg
    parts += supervisor

    [supervisor]
    recipe=collective.recipe.supervisor
    programs=
      10 demo ${buildout:directory}/bin/paster [serve ${pasterini:output}] ${buildout:directory} true

    [pasterini]
    port = 80


Deployment buildout
-------------------

Now create a third buildout file, called ``buildout.cfg``. This will be our
development/deployment buildout ::

    [buildout]
    extends = base.cfg
    parts += host1

    [host1]
    recipe = collective.hostout
    host = myhost.com
    hostos = ubuntu
    user = myusername
    path = /var/buildout/demo
    buildout = prod.cfg
    post-commands = bin/supervisord
    python-version = 2.6
    buildout-group = mygroupname

This buildout part will install a script which will deploy ``prod.cfg``
along with ``hellowsgi`` to the remote path ``/var/buildout/demo`` on our
server ``myhost.com`` ::

    $ bin/buildout
    Develop: '.../src/hellowsgi'
    Uninstalling host1.
    Installing demo.
    Installing host1.

As part of the buildout process, hostout will automatically save the
versions of all the eggs in your development buildout in a file called
``hostoutversions.cfg`` and will pin them for you during deployment. This
ensures that the production buildout will be running the same software as
you have tested locally. Remember to manually version pin any additional
eggs you use in your ``prod.cfg`` as these will not be pinned for you.

Running hostout deploy for the first time
-----------------------------------------

The ``bin/hostout`` command takes three kinds of parameters ::

   hostout [hostname(s)] [commands] [command arguments]

in our case we will run ::

   $ bin/hostout host1 deploy

The first thing this command will do, is to ask you your password and
attempt to login in to your server. It will then look for
``/var/buildout/demo/bin/buildout`` and when it doesn't find it it will
automatically run another hostout command called ``bootstrap``.

Bootstrap is further broken down into three commands: ``bootstrap_users``,
``bootstrap_python`` and ``bootstrap_buildout``. These will create an
additional ``buildout-user`` to build and run your application, install
basic system packages needed to run buildout, and install buildout into your
remote path.  It will attempt to detect which version of linux your server
is running to find the system python, but if this fails it will attempt to
compile python from source.  The version of python used will match the major
version of python which your development buildout uses.

Deploying and re-deploying
--------------------------

Once hostout bootstrap has ensured a working remote buildout, deployment
will continue by running the following commands:

1. ``uploadeggs``: Any *develop* eggs are released as eggs and uploaded to
   the server. These will be uploaded directly into the buildout's
   ``buildout-cache/downloads/dist`` directory which buildout uses to find
   packages before looking up the package index.

   It's very important the packages under development work when packaged,
   i.e. are capable of being packaged via ``python setup.py sdist``. A
   common mistake is to rely on setuptools to automatically detect which
   files should be included but not having the correct setuptools SCM
   helpers installed if you are using git or hg; e.g. for git do
   ``easy_install setuptools-git``.  This will also upload a ``pinned.cfg``
   which contains the generated version numbers for the packages under
   development that have been uploaded.

2. ``uploadbuildout``: The relevant ``.cfg`` files and any files/directories
   in the ``include`` parameter are synced to the remote server.

3. ``buildout``: Upload a final ``pinned.cfg`` which includes the generated
   development package versions pins and all the versions of all the
   dependencies of the development buildout from where the system is being
   deployed from. These discovered pinned versions are recorded during the
   local buildout process by the hostout recipe and recorded in a local
   ``hostoutversions.cfg`` file.  Buildout is then run on the remote
   production buildout.

   If you continue to develop your application you can run 
   ``hostout deploy`` each time and it will only upload the eggs that have
   changed and buildout will only reinstall changed parts of the buildout.

In our example above, deployment would look something like this ::

    $ bin/hostout host1 deploy
    running clean
    ...
    creating '...example-0.0.0dev_....egg' and adding '...' to it
    ...
    Hostout: Running command 'predeploy' from 'collective.hostout'
    ...
    Hostout: Running command 'uploadeggs' from 'collective.hostout'
    Hostout: Preparing eggs for transport
    Hostout: Develop egg src/demo changed. Releasing with hash ...
    Hostout: Eggs to transport:
    	demo = 0.0.0dev-...
    ...
    Hostout: Running command 'uploadbuildout' from 'collective.hostout'
    ...
    Hostout: Running command 'buildout' from 'collective/hostout'
    ...
    Hostout: Running command 'postdeploy' from 'collective/hostout'
    ...

Now if you visit ``myhost.com`` you will see your web application shared with
the world.


Hostout docker
**************

Hostout also integrates with docker to help build custom docker images
based on a local buildout.

First add your hostout config into your local buildout in order to generate
your hostout configuration.

```
[hostout]
recipe = collective.hostout
eggs =
  collective.hostout[docker]
extends =
  hostout.supervisor
versionsfile=hostoutversions.cfg
include =
hostos=ubuntu


[app]
<=
    hostout
extends =
buildout =
    buildout.cfg
parts =
    instance1
post-commands = ./bin/instance1 fg

[db]
<=
    hostout
extends =
buildout =
    buildout.cfg
parts =
    zeo
post-commands = ./bin/zeo fg
```

Now you can rerun buildout and then generate your docker image

```
$ bin/buildout -c dockerplone_devel.cfg
$ bin/hostout app db docker
```

This will use docker apis to generate two images, ```hostout/app``` and
```hostout/db```. It works similar to buildout in that if you buildout doesn't
complete then you can rerun the docker hostout command again and it will
continue where it left off. If you used your own DockerFile you would have
to ensure your buildout didn't fail as it will roll back the whole buildout
and you will start a new buildout each time you retry.

The image created will use ```post-commands``` hostout configuration to start
your process. Before startup it will also rerun your buildout in offline mode.
This allows your buildout to rewrite itself using environment variables. For
the following buildout allows a zope instance to be dynamically reconfigred
to connect to the zeo server using ```gocept.recipe.env```

```
[env]
recipe = gocept.recipe.env
# set defaults
ZEO_PORT_8100_TCP_ADDR = 0.0.0.0
ZEO_PORT_8100_TCP_PORT = 8100

[instance1]
recipe = plone.recipe.zope2instance
http-address = 0.0.0.0:8080
user=admin:admin
zeo-client = on
zeo-address =  ${env:ZEO_PORT_8100_TCP_ADDR}:${env:ZEO_PORT_8100_TCP_PORT}
shared-blob = off

[zeo]
recipe = plone.recipe.zeoserver
zeo-address = 0.0.0.0:8100
zeo-var = ${buildout:directory}/var
blob-storage = ${zeo:zeo-var}/blobstorage
```

If we use the following docker-compose.yml then the link will set an env variable
for the exposed port on the zeo server. This will then override the zope2 instance
during the startup buildout.

```
app:
  image: hostout/app
  ports:
   - "8080"
  volumes_from: app_var
  links:
   - db:zeo

db:
  image: hostout/db
  expose:
   - "8100"
  volumes_from: db_var


app_var:
  image: hostout/app # saves space and gets permissions right
  command: /bin/true # don't want hostout command to run
  volumes:
   - /var/buildout/app/var

db_var:
  image: hostout/db # saves space and gets permissions right
  command: /bin/true # don't want zeo to run
  volumes:
   - /var/buildout/db/var
```







Other built-in Commands
***********************

Hostout comes with a set of helpful commands. You can show this list by not
specifying any command at all. The list of commands will vary depending on
what fabfiles your hostout references. ::

   $ bin/hostout host1
   cmdline is: bin/hostout host1 [host2...] [all] cmd1 [cmd2...] [arg1 arg2...]
   Valid commands are:
     bootstrap        : Install python and users needed to run buildout
     bootstrap_python :
     bootstrap_users  : create buildout and the effective user and allow hostout access
     buildout         : Run the buildout on the remote server
     deploy           : predeploy, uploadeggs, uploadbuildout, buildout and then postdeploy
     postdeploy       : Perform any final plugin tasks
     predeploy        : Install buildout and its dependencies if needed. Hookpoint for plugins
     setowners        : Ensure ownership and permissions are correct on buildout and cache
     run              : Execute cmd on remote as login user
     sudo             : Execute cmd on remote as root user
     uploadbuildout   : Upload buildout pinned to local picked versions + uploaded eggs
     uploadeggs       : Any develop eggs are released as eggs and uploaded to the server


The ``run`` command is helpful to run quick remote commands as the buildout
user on the remote host ::

    $> bin/hostout host1 run pwd
    Hostout: Running command 'run' from collective.hostout
    Logging into the following hosts as root:
        127.0.0.1
    [127.0.0.1] run: sh -c "cd /var/host1 && pwd"
    [127.0.0.1] out: ...
    Done.

We can also use our login user and password to run quick sudo commands ::

    $> bin/hostout host1 sudo cat /etc/hosts
    Hostout: Running command 'sudo' from collective.hostout
    Logging into the following hosts as root:
        127.0.0.1
    [127.0.0.1] run: sh -c "cd /var/host1 && cat/etc/hosts"
    [127.0.0.1] out: ...
    Done.


Detailed Hostout Options
************************

Basic Options
-------------

``host``
  the IP or hostname of the host to deploy to. by default it will connect to
  port 22 using ssh.  You can override the port by using ``hostname:port``

``user``
  The user as which hostout will attempt to login to your host. Will read a
  user's ssh config to get a default.

``password``
  The password for the login user. If not given then hostout will ask each
  time.

``identity-file``
  A public key for the login user.

``extends``
  Specifies another part which contains defaults for this hostout.

``fabfiles``
  Path to fabric files that contain commands which can then be called from
  the hostout script. Commands can access hostout options via
  ``hostout.options`` from the fabric environment.


Deploy options
--------------

``buildout``
  The configuration file you wish to build on the remote host. Note that
  this doesn't have to be the same ``.cfg`` as the hostout section is in,
  but the versions of the eggs will be determined from the buildout with the
  ``hostout`` section in. Defaults to ``buildout.cfg``.


``path``
  The absolute path on the remote host where the buildout will be created.
  Defaults to ``'/var/buildout/%s'%name``, where ``name`` is the name of the
  *part* which defines this host.

``pre-commands``
  A series of shell commands executed as root before the buildout is run.
  You can use this to shut down your application, or to help prepare the
  environment for buildout. If these commands fail they will be ignored.

``post-commands``
  A series of shell commands executed as root after the buildout is run. You
  can use this to startup your application. If these commands fail they will
  be ignored.

``sudo-parts``
  Buildout parts which will be installed after the main buildout has been
  run. These will be run as root.

``parts``
  Runs the buildout with a ``parts`` value equal to this.

``include``
  Additional configuration files or directories needed to run this buildout.

``buildout-cache``
  If you want to override the default location for the ``buildout-cache`` on
  the host.

``python-version``
  The version of python to install during bootstrapping. (Mandatory.)

``hostos``
  Over which platform specific bootstrap_python command is called. For
  instance if ``hostos=redhat``, bootstrap_python_redhat will be called to
  use "yum" to install python and other developer tools. This paramter is
  also
  used in hostout.cloud_ to pick which VM to create.


Users and logins
----------------

The bootstrap_users command is called as part of the bootstrap process which
is called if no buildout has already been bootstrapped on the remote server.
This command will login using "user" (the user should have sudo rights) and
create two additional users and a group which joins them.

``effective-user``
  This user will own the buildouts var files. This allows the application to
  write to database files in the var directory but not be allowed to write
  to any other part of the buildout code.

``buildout-user``
  The user which will own the buildout files. During bootstrap this user
  will be created and be given a ssh key such that hostout can login and run
  buildout using this account.

``buildout-group``
  A group which will own the buildout files including the var files. This
  group is created if needed in the bootstrap_users command. (Mandatory.)

In addition the private key will be read from the location ``identity_file``
and be used to create a passwordless login for the ``buildout-user`` account
by copying the public key into the ``authorized_keys`` file of the
buildout_user account. If no file exists for ``identity_file`` a DSA private
key is created for you in the file ``${hostname}_key`` in the buildout
directory.  During a normal deployment all steps are run as the
*buildout-user* so there is no need to use the ``user`` account and
therefore supply a password. The exception to this is if you specify
``pre-deploy``, ``post-deploy`` or ``sudo-parts`` steps or have to bootstrap
the server.  These require the use of the sudo-capable ``user`` account.  If
you'd like to share the ability to deploy your application with others, one
way to do this is to simply checkin the private key file specified by
``identity_file`` along with your buildout. If you do share deployment,
remember to pin your eggs in your buildout so the result is consistent no
matter where  it is deployed from.  One trick you can use to achieve this is
to add ``hostoutversions.cfg`` to the ``extends`` of your buildout and
commit ``hostoutversions.cfg`` to your source control as well.



Integrating Fabric into buildout
********************************

Hostout uses fabric files. Fabric is an easy way to write python that
calls commands on a host over ssh.


Here is a basic fabfile which will echo two variables on the remote server.


>>> write('fabfile.py',"""
...
... from fabric import api
... from fabric.api import run
...
... def echo(cmdline1):
...    option1 = api.env.option1
...    run("echo '%s %s'" % (option1, cmdline1) )
...
... """)

Using hostout we can predefine some of the fabric scripts parameters as well
as install the fabric runner. Each hostout part in your ``buildout.cfg``
represents a connection to a server at a given path.

>>> write('buildout.cfg',
... """
... [buildout]
... parts = host1
...
... [host1]
... recipe = collective.hostout
... host = 127.0.0.1:10022
... fabfiles = fabfile.py
... option1 = buildout
... user = root
... password = root
... path = /var/host1
...
... """ )

If you don't include your password you will be prompted for it later.

When we run buildout a special fabric runner will be installed called
``bin/hostout``

>>> print system('bin/buildout -N')
Installing host1.
Generated script '/sample-buildout/bin/hostout'.


>>> print system('bin/hostout')
cmdline is: bin/hostout host1 [host2...] [all] cmd1 [cmd2...] [arg1 arg2...]
Valid hosts are: host1

We can run our fabfile by providing the

- host which refers to the part name in ``buildout.cfg``,
- command which refers to the method name in the fabfile,
- any other options we want to pass to the command.

Note: We can run multiple commands on one or more hosts using a single
commandline.

In our example

>>> print system('bin/hostout host1 echo "is cool"')
Hostout: Running command 'echo' from 'fabfile.py'
Logging into the following hosts as root:
    127.0.0.1
[127.0.0.1] run: echo 'cd /var/host1 && buildout is cool'
[127.0.0.1] out: ...
Done.

Note that we combined information from our buildout with
commandline paramaters to determine the exact command sent
to our server.

Making a hostout plugin
-----------------------

It can be very helpful to package up our fabfiles for others to use.

Hostout Plugins are eggs with three parts:

1. Fabric script

2. A zc.buildout recipe to initialise the parameters of the fabric file
   commands

3. Entry points for both the recipe and the fabric scripts

>>>    entry_points = {'zc.buildout': ['default = hostout.myplugin:Recipe',],
...                    'fabric': ['fabfile = hostout.myplugin.fabfile']
...                    },

Once packaged and released others can add your plugin to their hostout e.g.

>>> write('buildout.cfg',
... """
... [buildout]
... parts = host1
...
... [host1]
... recipe = collective.hostout
... extends = hostout.myplugin
... param1 = blah
... """ )

>>> print system('bin/buildout')

>>> print system('bin/hostout host1')
cmdline is: bin/hostout host1 [host2...] [all] cmd1 [cmd2...] [arg1 arg2...]
Valid commands are:
...
   mycommand        : example of command from hostout.myplugin

Your fabfile can get access parameters passed in the commandline by defining
them in your function; e.g. ::

    def mycommand(cmdline_param1, cmdline_param2):
        pass

Your fabfile commands can override any of the standard hostout commands. For
instance if you which your plugin to hook into the predeploy process then
just add a predeploy function to your ``fabfile.py`` ::

    def predeploy():
       api.env.superfun()

It is important when overridding to call the "superfun" function so any
overridden functions are also called.

You can also call any other hostout functions from your command ::

    def mycommand():
      api.env.hostout.deploy()

The options set in the buildout part are available via the Fabric
``api.env`` variable and also via ``api.env.hostout.options``.


.. TODO: Example of echo plugin

Using fabric plugins
--------------------

You use commands others have made via the *extends* option.  Name a buildout
recipe egg in the extends option and buildout will download and merge any
fabfiles and other configuration options from that recipe into your current
hostout configuration.  The following are examples of built-in plugins.
Others are available on pypi.

hostout.cloud_
  Will create VM instances automatically for you on many popular hosting
  services such as Amazon, Rackspace and Slicehost

hostout.supervisor_
  Will stop a supervisor before buildout is run and restart it afterwards.
  It provides some short commands to quickly manage your applications from
  your hostout commandline.




Why hostout
***********

Managing multiple environments can be a real pain and a barrier to
development.  Hostout puts all of the settings for all of your environments
in an easy-to-manage format.

Compared to

SilverLining
 Hostout allows you to deploy many different kinds of applications instead
 of just wsgi-based python apps. Buildout lets you define the installation
 of almost any kind of application.

Puppet
 TODO

mr.awesome
 TODO

Fabric
 TODO

Egg Proxies
 TODO



Using hostout with a python2.4 buildout
***************************************

Hostout itself requires python 2.6. However it is possible to use hostout
with a buildout that requires python 2.4 by using buildout's support for
different python interpreters.

>>> write('buildout.cfg',
... """
... [buildout]
... parts = host1
...
... [host1]
... recipe = collective.hostout
... host = 127.0.0.1:10022
... python = python26
...
... [python26]
... executable = /path/to/your/python2.6/binary
...
... """ )

or alternatively if you don't want to use your local python you can get
buildout to build it for you.


>>> write('buildout.cfg',
... """
... [buildout]
... parts = host1
...
... [host1]
... recipe = collective.hostout
... host = 127.0.0.1:10022
... python = python26
...
... [python26]
... recipe = zc.recipe.cmmi
... url = http://www.python.org/ftp/python/2.6.1/Python-2.6.1.tgz
... executable = ${buildout:directory}/parts/python/bin/python2.6
... extra_options=
...    --enable-unicode=ucs4
...    --with-threads
...    --with-readline
...
... """ )



Credits
*******

Dylan Jay ( software at pretaweb_ dot com )


.. _recipe: http://pypi.python.org/pypi/zc.buildout#recipes
.. _Fabric: http://fabfile.org
.. _buildout: http://www.buildout.org
.. _pretaweb: http://www.pretaweb.com
.. _supervisord: http://supervisord.org/
.. _libcloud: http://incubator.apache.org/libcloud/
.. _hostout.cloud: http://pypi.python.org/pypi/hostout.cloud
.. _hostout.supervisor: http://pypi.python.org/pypi/hostout.supervisor



