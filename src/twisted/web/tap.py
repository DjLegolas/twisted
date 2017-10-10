# -*- test-case-name: twisted.web.test.test_tap -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Support for creating a service which runs a web server.
"""

from __future__ import absolute_import, division

import os
import warnings

import incremental

from twisted.application import service, strports
from twisted.internet import interfaces, reactor
from twisted.python import usage, reflect, threadpool, deprecate
from twisted.spread import pb
from twisted.web import distrib
from twisted.web import resource, server, static, script, demo, wsgi
from twisted.web import twcgi

class Options(usage.Options):
    """
    Define the options accepted by the I{twistd web} plugin.
    """
    synopsis = "[web options]"

    optParameters = [["logfile", "l", None,
                      "Path to web CLF (Combined Log Format) log file."],
                     ["https", None, None,
                      "(DEPRECATED: use --http) "
                      "Port to listen on for Secure HTTP."],
                     ["certificate", "c", "server.pem",
                      "(DEPRECATED: use --http) "
                      "SSL certificate to use for HTTPS. "],
                     ["privkey", "k", "server.pem",
                      "(DEPRECATED: use --http) "
                      "SSL certificate to use for HTTPS."],
                     ]

    optFlags = [
        ["notracebacks", "n", (
            "Do not display tracebacks in broken web pages. Displaying "
            "tracebacks to users may be security risk!")],
    ]

    optFlags.append([
        "personal", "",
        "Instead of generating a webserver, generate a "
        "ResourcePublisher which listens on  the port given by "
        "--http, or ~/%s " % (distrib.UserDirectory.userSocketName,) +
        "if --http is not specified."])

    compData = usage.Completions(
                   optActions={"logfile" : usage.CompleteFiles("*.log"),
                               "certificate" : usage.CompleteFiles("*.pem"),
                               "privkey" : usage.CompleteFiles("*.pem")}
                   )

    longdesc = """\
This starts a webserver.  If you specify no arguments, it will be a
demo webserver that has the Test class from twisted.web.demo in it."""

    def __init__(self):
        usage.Options.__init__(self)
        self['indexes'] = []
        self['root'] = None
        self['extraHeaders'] = []
        self['ports'] = []

    @deprecate.deprecated(incremental.Version("Twisted", "NEXT", 0, 0))
    def opt_port(self, port):
        """
        (DEPRECATED: use --http)
        Strports description of port to start the server on
                     ["port", "p", None,

        @param port: the strport description
        """
        self['port'] = port

    # See https://github.com/twisted/twistedchecker/issues/134
    opt_port.__doc__ = opt_port.__doc__.split('@param')[0]

    opt_p = opt_port

    @deprecate.deprecated(incremental.Version("Twisted", "NEXT", 0, 0))
    def opt_https(self, port):
        """
        Port to listen on for Secure HTTP.
        (DEPRECATED: use --http)

        @param port: the strport description
        """
        self['https'] = port

    opt_https.__doc__ = opt_https.__doc__.split('@param')[0]

    def opt_http(self, port):
        """
        Add an strports description of port to start the server on.
        [default: tcp:8080]

        @param port: the strport description
        """
        self['ports'].append(port)

    # See https://github.com/twisted/twistedchecker/issues/134
    opt_http.__doc__ = opt_http.__doc__.split('@param')[0]


    def opt_index(self, indexName):
        """
        Add the name of a file used to check for directory indexes.
        [default: index, index.html]
        """
        self['indexes'].append(indexName)

    opt_i = opt_index


    def opt_user(self):
        """
        Makes a server with ~/public_html and ~/.twistd-web-pb support for
        users.
        """
        self['root'] = distrib.UserDirectory()

    opt_u = opt_user


    def opt_path(self, path):
        """
        <path> is either a specific file or a directory to be set as the root
        of the web server. Use this if you have a directory full of HTML, cgi,
        epy, or rpy files or any other files that you want to be served up raw.
        """
        self['root'] = static.File(os.path.abspath(path))
        self['root'].processors = {
            '.epy': script.PythonScript,
            '.rpy': script.ResourceScript,
        }
        self['root'].processors['.cgi'] = twcgi.CGIScript


    def opt_processor(self, proc):
        """
        `ext=class' where `class' is added as a Processor for files ending
        with `ext'.
        """
        if not isinstance(self['root'], static.File):
            raise usage.UsageError(
                "You can only use --processor after --path.")
        ext, klass = proc.split('=', 1)
        self['root'].processors[ext] = reflect.namedClass(klass)


    def opt_class(self, className):
        """
        Create a Resource subclass with a zero-argument constructor.
        """
        classObj = reflect.namedClass(className)
        self['root'] = classObj()


    def opt_resource_script(self, name):
        """
        An .rpy file to be used as the root resource of the webserver.
        """
        self['root'] = script.ResourceScriptWrapper(name)


    def opt_wsgi(self, name):
        """
        The FQPN of a WSGI application object to serve as the root resource of
        the webserver.
        """
        try:
            application = reflect.namedAny(name)
        except (AttributeError, ValueError):
            raise usage.UsageError("No such WSGI application: %r" % (name,))
        pool = threadpool.ThreadPool()
        reactor.callWhenRunning(pool.start)
        reactor.addSystemEventTrigger('after', 'shutdown', pool.stop)
        self['root'] = wsgi.WSGIResource(reactor, pool, application)


    def opt_mime_type(self, defaultType):
        """
        Specify the default mime-type for static files.
        """
        if not isinstance(self['root'], static.File):
            raise usage.UsageError(
                "You can only use --mime_type after --path.")
        self['root'].defaultType = defaultType
    opt_m = opt_mime_type


    def opt_allow_ignore_ext(self):
        """
        Specify whether or not a request for 'foo' should return 'foo.ext'
        """
        if not isinstance(self['root'], static.File):
            raise usage.UsageError("You can only use --allow_ignore_ext "
                                   "after --path.")
        self['root'].ignoreExt('*')


    def opt_ignore_ext(self, ext):
        """
        Specify an extension to ignore.  These will be processed in order.
        """
        if not isinstance(self['root'], static.File):
            raise usage.UsageError("You can only use --ignore_ext "
                                   "after --path.")
        self['root'].ignoreExt(ext)


    def opt_add_header(self, header):
        """
        Specify an additional header to be included in all responses. Specified
        as "HeaderName: HeaderValue".
        """
        name, value = header.split(':', 1)
        self['extraHeaders'].append((name.strip(), value.strip()))


    def postOptions(self):
        """
        Set up conditional defaults and check for dependencies.

        If SSL is not available but an HTTPS server was configured, raise a
        L{UsageError} indicating that this is not possible.

        If no server port was supplied, select a default appropriate for the
        other options supplied.
        """
        if self['port']:
            self['ports'].append(self['port'])
        if self['https']:
            try:
                reflect.namedModule('OpenSSL.SSL')
            except ImportError:
                raise usage.UsageError("SSL support not installed")
            sslStrport = 'ssl:port={}:privateKey={}:certKey={}'.format(
                             self['https'],
                             self['privkey'],
                             self['certificate'],
                         )
            self['ports'].append(sslStrport)
        if len(self['ports']) == 0:
            if self['personal']:
                path = os.path.expanduser(
                    os.path.join('~', distrib.UserDirectory.userSocketName))
                self['ports'].append('unix:' + path)
            else:
                self['ports'].append('tcp:8080')



def makePersonalServerFactory(site):
    """
    Create and return a factory which will respond to I{distrib} requests
    against the given site.

    @type site: L{twisted.web.server.Site}
    @rtype: L{twisted.internet.protocol.Factory}
    """
    return pb.PBServerFactory(distrib.ResourcePublisher(site))



class _AddHeadersResource(resource.Resource):
    def __init__(self, originalResource, headers):
        self._originalResource = originalResource
        self._headers = headers


    def getChildWithDefault(self, name, request):
        for k, v in self._headers:
            request.responseHeaders.addRawHeader(k, v)
        return self._originalResource.getChildWithDefault(name, request)



def makeService(config):
    s = service.MultiService()
    if config['root']:
        root = config['root']
        if config['indexes']:
            config['root'].indexNames = config['indexes']
    else:
        # This really ought to be web.Admin or something
        root = demo.Test()

    if isinstance(root, static.File):
        root.registry.setComponent(interfaces.IServiceCollection, s)

    if config['extraHeaders']:
        root = _AddHeadersResource(root, config['extraHeaders'])

    if config['logfile']:
        site = server.Site(root, logPath=config['logfile'])
    else:
        site = server.Site(root)

    site.displayTracebacks = not config["notracebacks"]

    if config['personal']:
        site = makePersonalServerFactory(site)
    for port in config['ports']:
        svc = strports.service(port, site)
        svc.setServiceParent(s)
    return s
