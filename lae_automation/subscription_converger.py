"""
This module implements a convergence service which keeps
Kubernetes configuration in line with customer subscriptions.

The service operates in a never-ending loop.  Each iteration of the
loop checks the Kubernetes configuration against the active customer
subscriptions.  If Kubernetes configuration is found which relates to
inactive subscriptions, it is removed.  If subscriptions are found
with no corresponding Kubernetes configuration, such is added for
them.
"""

import attr

from eliot import startAction
from eliot.twisted import DeferredContext

from twisted.internet.defer import inlineCallbacks, maybeDeferred
from twisted.application.internet import TimerService
from twisted.python.usage import Options as _Options
from twisted.web.client import Agent

import pykube

from .route53 import get_route53_client
from .signup import DeploymentConfiguration
from .subscription_manager import Client as SMClient
from .containers import (
    configmap_name, deployment_name,
    create_configuration, create_deployment,
    add_subscription_to_service, remove_subscription_from_service
)

class Options(_Options):
    optParameters = [
        ("endpoint", "e", None, "The root URL of the subscription manager service."),
    ]

def makeService(options):
    from twisted.internet import reactor
    agent = Agent(reactor)
    subscription_client = SMClient(endpoint=options["endpoint"], agent=agent)

    k8s_client = pykube.HTTPClient.from_service_account()

    config = DeploymentConfiguration()
    
    return TimerService(
        1.0,
        divert_errors_to_log(converge), config, subscription_client, k8s_client,
    )

def divert_errors_to_log(f):
    def g(*a, **kw):
        action = startAction("subscription_converger:" + f.__name__)
        with action.context():
            d = DeferredContext(maybeDeferred(f, *a, **kw))
            d.addFinishAction()
            # The failure was logged by the above.  Now squash it.
            d.addErrback(lambda err: None)
            return d.result
    return g


def get_customer_grid_service(k8s):
    return pykube.Service.objects(k8s).filter(
        provider="LeastAuthority",
        app="s4",
        component="customer-tahoe-lafs"
    )

def get_customer_grid_deployments(k8s):
    return pykube.Deployment.objects(k8s).filter(
        provider="LeastAuthority",
        app="s4",
        component="customer-tahoe-lafs"
    )

@inlineCallbacks
def converge(config, subscriptions, k8s, aws):
    # Create and destroy deployments as necessary.  Use the
    # subscription manager to find out what subscriptions are active
    # and use look at the Kubernetes configuration to find out what
    # subscription-derived deployments exist.  Also detect port
    # mis-configurations and correct them.
    active_subscriptions = {
        subscription.id: subscription
        for subscription
        in (yield subscriptions.list())
    }
    configured_deployments = get_customer_grid_deployments(k8s)
    configured_service = get_customer_grid_service(k8s)

    to_create = set(active_subscriptions)
    to_delete = set()

    for deployment in configured_deployments:
        subscription_id = deployment["metadata"]["subscription"]
        try:
            subscription = active_subscriptions[subscription_id]
        except KeyError:
            to_delete.add(subscription_id)
            continue

        to_create.remove(subscription)

        if deployment["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] != subscription.introducer_port_number:
            to_delete.add(subscription_id)
            to_create.add(subscription)
        elif deployment["spec"]["template"]["spec"]["containers"][1]["ports"][0]["containerPort"] != subscription.storage_port_number:
            to_delete.add(subscription_id)
            to_create.add(subscription)

    configmaps = list(
        create_configuration(config, details)
        for details
        in to_create
    )
    deployments = list(
        create_deployment(config, details)
        for details
        in to_create
    )
    service = apply_service_changes(configured_service, to_delete, to_create)

    route53 = get_route53_client(aws)
    
    route53.destroy(to_delete)
    k8s.destroy(list(deployment_name(sid) for sid in to_delete))
    k8s.destroy(list(configmap_name(sid) for sid in to_delete))

    k8s.create(configmaps)
    k8s.create(deployments)
    k8s.apply(service)
    route53.create(to_create)


def apply_service_changes(service, to_delete, to_create):
    with_deletions = reduce(
        remove_subscription_from_service, service, to_delete,
    )
    with_creations = reduce(
        add_subscription_to_service, with_deletions, to_create,
    )
    return with_creations
    

# def converge(subscriptions, k8s, aws):
#     return serially([
#         # Create and destroy deployments as necessary.  Use the
#         # subscription manager to find out what subscriptions are
#         # active and use look at the Kubernetes configuration to find
#         # out what subscription-derived deployments exist.
#         lambda: converge_deployments(subscriptions, k8s),

#         # Converge the rest of the system based on the result of that.
#         make_fan_out([
#             # Update the Kubernetes service so that it exposes the
#             # subscription-derived Deployments that now exist.
#             lambda deployments: converge_service(deployments, k8s),

#             # If there were changes, update the Route53 configuration.
#             lambda deployments: converge_route53(deployments, aws),
#         ]),
#     ])


# def converge_deployments(subscriptions, k8s):
#     active_subscriptions = subscriptions.list().addCallback(set)
#     configured_deployments = get_subscription_service(k8s)

#     d = DeferredList([active_subscriptions, configured_deployments])
#     d.addCallback()
#     d.addCallback(enact_configuration, k8s)
#     return d




# def converge_logic(desired, service):
#     # converge_deployment
#     # converge_configmap
#     # converge_route53
#     return converge_service(desired, service)

def get_ports(service):
    return service["spec"]["ports"]

def get_configured_subscriptions(service):
    # Every pair of i-... s-... ports is a configured subscription.

    def names(ports):
        return (port["name"] for port in ports)
    port_names = {
        name
        for name
        in names(get_ports(service))
        if name.startswith("i-") or name.startswith("s-")
    }

    def ids(names):
        return (name[2:] for name in names)
    subscriptions = {
        sid
        for sid
        in ids(port_names)
        if "i-" + sid in port_names and "s-" + sid in port_names
    }
    return subscriptions    


def converge_service(desired, service):
    actual = get_configured_subscriptions(service)
    changes = compute_changes(desired, actual)
    # XXX Cannot update configuration without retrieving more state.
    new_service = update_configuration(changes, service)
    return new_service


@attr.s(frozen=True)
class Delete(object):
    subscription = attr.ib()

    def enact(self, service):
        
        return remove_subscription_from_service(service, self.subscription)

@attr.s(frozen=True)
class Create(object):
    subscription = attr.ib()

    def enact(self, service):
        return add_subscription_to_service(service, self.subscription)


def compute_changes(desired, actual):
    extra = actual - desired
    missing = desired - actual

    return map(Create, extra) + map(Delete, missing)

def update_configuration(changes, service):
    for change in changes:
        service = change.enact(service)
    return service

# def enact_configuration(service, k8s):
#     # XXX No such API
#     k8s.replace(service)

# @inlineCallbacks
# def serially(operations):
#     """
#     Call each of the given functions, one after the other.

#     The first function is called with no arguments.  Subsequent
#     functions are called with the result of the previous function.

#     If a function returns a Deferred, the next function is not called
#     until the Deferred fires.  Then it is called with the result of
#     the Deferred.

#     If a function raises an exception, no further functions are called
#     and the Deferred returned by this function fires with a Failure.

#     If all functions are executed without exception, the Deferred
#     returned by this function fires with the result of the last
#     function.
#     """
#     operations_iterator = iter(operations)
#     try:
#         first = next(operations_iterator)
#     except StopIteration:
#         return
#     result = yield first()
#     for op in operations_iterator:
#         result = yield op(result)
#     yield result


# def make_fan_out(operations):
#     """
#     Convert an iterable of functions to a single function which calls
#     each function.
#     """
#     def fan_out(result):
#         return DeferredList(list(
#             maybeDeferred(op, result)
#             for op
#             in operations
#         ))
#     return fan_out
