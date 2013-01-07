from __future__ import absolute_import

from django.core import urlresolvers
from django.template.base import Template
from django.template.response import TemplateResponse
from django.core.handlers.base import BaseHandler

from .base import RequestTrace
from .monkey_patching import monkeypatch_method

from functools import wraps
import traceback


def trace(function, action_type, label):
    @wraps(function)
    def wrapper(*args, **kwargs):
        stacktracer = RequestTrace.instance().stacktracer
        stacktracer.push_stack(action_type, label)
        try:
            return function(*args, **kwargs)
        finally:
            stacktracer.pop_stack()
    return wrapper


def patch_function_list(functions, action_type, format_string):
    for i, func in enumerate(functions):
        functions[i] = trace(func, action_type, format_string % (func.im_class.__name__),)


def wrap_middleware_with_tracers(request_handler):
    patch_function_list(request_handler._request_middleware, 'MIDDLEWARE_REQUEST', 'Middleware: %s (request)')
    patch_function_list(request_handler._view_middleware, 'MIDDLEWARE_VIEW', 'Middleware: %s (view)')
    patch_function_list(request_handler._template_response_middleware, 'MIDDLEWARE_TEMPLATE_RESPONSE', 'Middleware: %s (template response)')
    patch_function_list(request_handler._response_middleware, 'MIDDLEWARE_RESPONSE', 'Middleware: %s (response)')
    patch_function_list(request_handler._exception_middleware, 'MIDDLEWARE_EXCEPTION', 'Middleware: %s (exeption)')


# The linter thinks the methods we monkeypatch are not used
# pylint: disable=W0612
def intercept_middleware():
    @monkeypatch_method(BaseHandler)
    def load_middleware(original, self, *args, **kwargs):
        original(self, *args, **kwargs)
        wrap_middleware_with_tracers(self)


def intercept_resolver_and_view():
    # The only way we can really wrap the view method is by replacing the implementation
    # of RegexURLResolver.resolve. It would be nice if django had more configurability here, but it does not.
    # However, we only want to replace it when invoked directly from the request handling stack, so we
    # inspect the callstack in __new__ and return either a normal object, or an instance of our proxying
    # class.
    real_resolver_cls = urlresolvers.RegexURLResolver
    class ProxyRegexURLResolverMetaClass(urlresolvers.RegexURLResolver.__class__):
        def __instancecheck__(self, instance):
            # Some places in django do a type check against RegexURLResolver and behave differently based on the result, so we have to
            # make sure the replacement class we plug in accepts instances of both the default and replaced types.
            return isinstance(instance, real_resolver_cls) or super(ProxyRegexURLResolverMetaClass, self).__instancecheck__(instance)
    class ProxyRegexURLResolver(object):
        __metaclass__ = ProxyRegexURLResolverMetaClass
        def __new__(cls, *args, **kwargs):
            real_object = real_resolver_cls(*args, **kwargs)
            stack = traceback.extract_stack()
            if stack[-2][2] == 'get_response':
                obj = super(ProxyRegexURLResolver, cls).__new__(cls)
                obj.other = real_object
                return obj
            else:
                return real_object
        def __getattr__(self, attr):
            return getattr(self.other, attr)
        def resolve(self, path):
            stack_tracer = RequestTrace.instance().stacktracer
            stack_tracer.push_stack('RESOLV', 'Resolving: ' + path)
            try:
                callbacks = self.other.resolve(path)
            finally:
                stack_tracer.pop_stack()
            # Replace the callback function with a traced copy so we can time how long the view takes.
            callbacks.func = trace(callbacks.func, 'VIEW', 'View: ' + callbacks.view_name)
            return callbacks
    urlresolvers.RegexURLResolver = ProxyRegexURLResolver


def intercept_template_methods():
    @monkeypatch_method(Template)
    def __init__(original, self, *args, **kwargs):
        name = args[2] if len(args) >= 3 else '<Unknown Template>'
        stack_tracer = RequestTrace.instance().stacktracer
        stack_tracer.push_stack('TEMPLATE_COMPILE', 'Compile template: ' + name)
        try:
            original(self, *args, **kwargs)
        finally:
            stack_tracer.pop_stack()

    @monkeypatch_method(Template)
    def render(original, self, *args, **kwargs):
        stack_tracer = RequestTrace.instance().stacktracer
        stack_tracer.push_stack('TEMPLATE_RENDER', 'Render template: ' + self.name)
        try:
            return original(self, *args, **kwargs)
        finally:
            stack_tracer.pop_stack()

    @monkeypatch_method(TemplateResponse)
    def resolve_context(original, self, *args, **kwargs):
        stack_tracer = RequestTrace.instance().stacktracer
        stack_tracer.push_stack('TEMPLATE_CONTEXT', 'Resolve context')
        try:
            return original(self, *args, **kwargs)
        finally:
            stack_tracer.pop_stack()


def init():
    intercept_middleware()
    intercept_resolver_and_view()
    intercept_template_methods()
