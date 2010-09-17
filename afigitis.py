#!/usr/bin/env python

from BaseHTTPServer import BaseHTTPRequestHandler as HTTP
import cgi
import cgitb; cgitb.enable()
import re
import urlparse
import string
import sys

class HTTPError(Exception):
	def __init__(self, status, message=None, *args):
		self.status = int(status)
		self.message = message
		self.args = args

	def __str__(self):
		message = 'HTTP %d: %s' % (self.status, HTTP.responses[self.status][0])
		if self.message: message += ' (' + (self.message % self.args) + ')'
		return message

class Router(object):
	def __init__(self):
		self.matcher = self.buildRouteMatcher()
		self.routes = []

	def addRoute(self, pattern, wsgiresponse):
		self.routes.append((pattern, wsgiresponse))

	@staticmethod
	def buildRouteMatcher():
		return re.compile('''
			\{						# Literal '{'
			(\w+)					# Path Node [a-z0-9_]
			(?::([^}]+))?	# Optional Pattern for Path Node
			\}						# Literal '}'
			''', re.VERBOSE)

	@staticmethod
	def compileRoute(matcher, route):
				def makeNamedPattern(match):
					return '(?P<%s>%s)' % (match.group(1), match.group(2) or '[^/]+')

				def consumeMatches(matcher, template):
					t = template
					for match in matcher.finditer(template):
						yield (re.escape(template[:match.start()]), makeNamedPattern(match))
						t = t[:m.end()]

					yield re.escape(t)

				return re.compile('^' + ''.join(consumeMatches(matcher, route)) + '$')

	def compileRoutes(self):
		'''Uses pattern match syntax described in: http://pythonpaste.org/webob/do-it-yourself.html'''
		self.compiledRoutes = [(self.compileRoute(self.matcher, template), response) for template, response in self.routes]

	def match(self, request):
		for matcher, response in self.compiledRoutes:
			match = matcher.match(request.path)
			if match:
				request.routervars = match.groupdict()
				return response(match.groupdict())
		else:
			raise ValueError

class QueryString(object):
	def __init__(self, queryString):
		self.query = cgi.parse_qs(queryString, True)

	def addArgument(self, argument, value):
		if argument in self.query:
			if type([]) == type(self.query[argument]):
				self.query[argument].append(value)
			else:
				self.query[argument] = [self.query[argument], value]
		else:
			self.query[argument] = value

	_DEFAULT_ARGUMENT = object()
	def getArgument(self, argument, default=_DEFAULT_ARGUMENT, strip=True):
		args = self.getArguments(argument, strip=strip)
		if not args:
			if default is self._DEFAULT_ARGUMENT:
				raise HTTPError(404, 'Missing argument ' + argument)
			return default
		return args[-1]

	def getArguments(self, argument, strip=True):
		values = self.query.get(argument, [])
		# drop ascii control characters
		values = [re.sub(r'[\x00-\x08\x0e-\x1f]', ' ', x) for x in values]
		if strip: values = [v.strip() for v in values]
		return values

class WSGIRequest(object):
	def __init__(self, environ):
		self.environ = environ
		self.method = environ.get('REQUEST_METHOD', 'GET').upper()
		self.headerTrans = string.maketrans('-_','  ')
		self.headers = {}

		self.parseHeaders()
		self.parseRequest()

	@staticmethod
	def _canonicalHeader(tr, header):
		return header.upper().translate(tr)

	def getHeader(self, header, default=None):
		header = self._canonicalHeader(self.headerTrans, header)
		if header in self.headers:
			return self.headers[header]
		else:
			return default

	def parseHeaders(self):
		for header in [_ for _ in self.environ if _.startswith('HTTP_')]:
			canonicalHeader = self._canonicalHeader(self.headerTrans, header[len('HTTP_'):])
			self.headers[canonicalHeader] = self.environ[header]

	def parseRequest(self):
		path = self.environ.get('PATH_INFO', '')
		self.scheme, netloc, path, query, self.fragment = urlparse.urlsplit(path, self.environ.get('wsgi.url_scheme', 'http'))
		if '@' in netloc:
			auth, host = netloc.split('@')
		else:
			auth, host = (None, netloc)

		if auth:
			try:
				self.username, self.password = auth.split(':')
			except ValueError:
				self.username, self.password = (auth, None)

		try:
			self.hostname, self.port = host.split(':')
		except ValueError:
			self.hostname = host
			self.port = self.scheme == 'https' and 443 or 80

		self.parameters = {}
		segments = []
		for unparsedSegment in path.split('/'):
			segmentAndParameters = unparsedSegment.split(';')
			segment, parameters = segmentAndParameters[0], segmentAndParameters[1:]
			if parameters: self.parameters[segment] = parameters
			segments.append(segment)

		self.path = '/'.join(segments)

		self.query = QueryString(self.environ.get('QUERY_STRING', ''))

		if self.method in ('POST', 'PUT'):
			try:
				contentType = cgi.parse_header(self.getHeader('content-type'))[0]
				contentLength = cgi.parse_header(self.getHeader('content-length'))[0]
				if contentType in ('application/x-www-form-encoded', 'multipart/form-data'):
					q = self.environ['wsgi.input'].read(int(contentLength))
					for argument, value in cgi.parse_qs(q, True).iteritems():
						if type([]) == type(value):
							for v in value:
								self.query.addArgument(argument, v)
						else:
							self.query.addArgument(argument, value)
			except:
				pass

class Response(object):
	def __init__(self, request, code = 200, headers = {}, response = []):
		self.request = request or WSGIRequest({})
		self.code = code
		self.headers = headers
		self.response = response
		if not response:
			getattr(self, 'do_' + self.request.method)()

	def addHeader(self, header, value):
		self.headers[header] = str(value)

	@property
	def status(self):
		return '%d %s' % (self.code, HTTP.responses[self.code][0])

	def write(self, data):
		self.response.append(data)

	def do_DELETE(self): raise HTTPError(405)
	def do_GET(self): raise HTTPError(405)
	def do_HEAD(self): raise HTTPError(405)
	def do_POST(self): raise HTTPError(405)
	def do_PUT(self): raise HTTPError(405)

class WSGIApplication(object):
	def __init__(self, router = None):
		self.router = router

	def parseConfiguration(self, configuration):
		pass

	def __call__(self, environ, start_response):
		'''Called by a WSGI compliant wrapper'''
		request = None
		try:
			request = WSGIRequest(environ)
			try:
				response = self.router.match(request)
			except ValueError:
				raise HTTPError(404, "The requested URL %s was not found on this server" % request.path)
		except HTTPError, error:
			response = Response(request, code = error.status, response = [error.message])
		except:
			response = Response(request, code = 500, headers = {'content-type': 'text/html'}, response = [cgitb.html(sys.exc_info())])

		contentLength = sum(map(lambda o: len(str(o)), response.response))
		response.addHeader('content-length', contentLength)

		start_response(response.status, [(k,v) for k,v in response.headers.iteritems()])
		return response.response

class Frontpage(Response):
	html = '''
<!DOCTYPE html>
<html lang="en">
<head>
<title>Afigitis</title>
</head>
<body>
</body>
</html>'''
	def do_GET(self):
		self.addHeader('content-type', 'text/html')
		self.write(self.html)

if __name__ == '__main__':
	import wsgiref.simple_server
	router = Router()
	router.addRoute('/', Frontpage)
	router.compileRoutes()
	application = WSGIApplication(router = router)
	httpd = wsgiref.simple_server.make_server('', 8080, application)
	httpd.serve_forever()