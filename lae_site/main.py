# before importing Twisted
import mimetypes
mimetypes.add_type("text/plain", ".rst")

if __name__ == '__main__':
    from sys import argv
    from twisted.internet.task import react
    from lae_site.main import main

    react(main, argv[1:])

import sys, os
import logging

import pem

from twisted.python.log import startLogging
from twisted.internet import ssl
from twisted.internet.defer import Deferred
from twisted.python.usage import UsageError, Options
from twisted.python.filepath import FilePath

from lae_site.handlers import make_site, make_redirector_site
from lae_site.handlers.submit_subscription import start

root_log = logging.getLogger(__name__)

class SiteOptions(Options):
    optFlags = [
        ("noredirect", None, "Disable the cleartext redirect-to-TLS site."),

        # TODO:
        # Make this HTTP-only.
        # Terminate TLS externally.
        # On K8S on AWS, consider using
        # http://kubernetes.io/docs/user-guide/services/#ssl-support-on-aws
        ("nossl", None, "Run the site on a cleartext HTTP server instead of over TLS. "),
    ]

    optParameters = [
        ("signup-furl-path", None, None, "A path to a file containing the signup furl.", FilePath),
        ("interest-path", None, None, "A path to a file to which contact information of people interested in products will be appended.", FilePath),
        ("stripe-secret-api-key-path", None, None, "A path to a file containing a Stripe API key.", FilePath),
        ("stripe-publishable-api-key-path", None, None, "A path to a file containing a publishable Stripe API key.", FilePath),
        ("subscriptions-path", None, None, "A path to a file to which new subscription details will be appended.", FilePath),
        ("service-confirmed-path", None, None, "A path to a file to which confirmed-service subscription details will be appended.", FilePath),
        ("site-logs-path", None, None, "A path to a file to which HTTP logs for the site will be written.", FilePath),

        ("port", None, 443, "The TCP port number on which to listen for TLS/HTTP requests.", int),
        ("redirectport", None, 80, "A TCP port number on which to run a redirect-to-TLS site.", int),
        ("redirect-to-port", None, None, "A TCP port number to which to redirect for the TLS site.", int),
    ]


    def postOptions(self):
        required_options = [
            "signup-furl-path",
            "interest-path",
            "stripe-secret-api-key-path",
            "stripe-publishable-api-key-path",
            "service-confirmed-path",
            "subscriptions-path",
            "site-logs-path",
        ]
        for option in required_options:
            if self[option] is None:
                raise UsageError("Missing required option --{}".format(option))


def main(reactor, *argv):
    o = SiteOptions()
    try:
        o.parseOptions(argv)
    except UsageError as e:
        raise SystemExit(str(e))

    logging.basicConfig(
        stream = sys.stdout,
        level = logging.DEBUG,
        format = '%(asctime)s %(levelname) 7s [%(name)-65s L%(lineno)d] %(message)s',
        datefmt = '%Y-%m-%dT%H:%M:%S%z',
        )

    startLogging(sys.stdout, setStdout=False)

    root_log.info('Listening on port {}...'.format(o["port"]))

    signup_furl = o["signup-furl-path"].getContent().strip()
    d = start(signup_furl)
    d.addCallback(
        lambda ignored: make_site(
            o["interest-path"],
            o["stripe-secret-api-key-path"].getContent().strip(),
            o["stripe-publishable-api-key-path"].getContent().strip(),
            o["service-confirmed-path"],
            o["subscriptions-path"],
            o["site-logs-path"],
        )
    )
    d.addCallback(
        lambda site: start_site(
            reactor,
            site,
            o["port"],
            not o["nossl"],
            not o["noredirect"], o["redirectport"], o["redirect-to-port"],
        )
    )
    d.addCallback(lambda ignored: Deferred())
    return d

def start_site(reactor, site, port, ssl_enabled, redirect, redirect_port, redirect_to_port):
    if ssl_enabled:
        root_log.info('SSL/TLS is enabled (start with --nossl to disable).')
        KEYFILE = '../secret_config/rapidssl/server.key'
        CERTFILE = '../secret_config/rapidssl/server.crt'
        assert os.path.exists(KEYFILE), "Private key file %s not found" % (KEYFILE,)
        assert os.path.exists(CERTFILE), "Certificate file %s not found" % (CERTFILE,)

        with open(KEYFILE) as keyFile:
            key = keyFile.read()

        certs = pem.parse_file(CERTFILE)
        cert = ssl.PrivateCertificate.loadPEM(str(key) + str(certs[0]))

        extraCertChain = [ssl.Certificate.loadPEM(str(certData)).original
                          for certData in certs[1:]]

        cert_options = ssl.CertificateOptions(
            privateKey=cert.privateKey.original,
            certificate=cert.original,
            extraCertChain=extraCertChain,
        )

        reactor.listenSSL(port, site, cert_options)

        if redirect:
            if redirect_to_port is None:
                redirect_to_port = port
            root_log.info('http->https redirector listening on port %d...' % (redirect_port,))
            reactor.listenTCP(redirect_port, make_redirector_site(redirect_to_port))
    else:
        root_log.info('SSL/TLS is disabled.')
        reactor.listenTCP(port, site)
