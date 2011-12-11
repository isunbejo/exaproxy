#!/usr/bin/env python
# encoding: utf-8
"""
process.py

Created by Thomas Mangin on 2011-11-29.
Copyright (c) 2011 Exa Networks. All rights reserved.
"""

from threading import Thread
import subprocess
import errno

import os
import time
import socket

from Queue import Empty

from .http import HTTPParser,regex

from .logger import Logger
logger = Logger()

from .configuration import Configuration
configuration = Configuration()

class Worker (HTTPParser,Thread):
	
	# TODO : if the program is a function, fork and run :)
	
	def __init__ (self, name, request_box, program):
		self.process = None                           # the forked program to handle classification

		# XXX: all this could raise things
		r, w = os.pipe()                              # pipe for communication with the main thread
		self.response_box_write = os.fdopen(w,'w')    # results are written here
		self.response_box_read = os.fdopen(r,'r')     # read from the main thread

		self.wid = name                               # a unique name
		self.creation = time.time()                   # when the thread was created
		self.last_worked = self.creation              # when the thread last picked a task
		self.request_box = request_box                # queue with HTTP headers to process

		self.program = program                        # the squid redirector program to fork 
		self.running = True                           # the thread is active
		Thread.__init__(self)

	def _createProcess (self):
		try:
			process = subprocess.Popen([self.program,],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				universal_newlines=True,
			)
			logger.worker('spawn process %s' % self.program, 'worker %d' % self.wid)
		except KeyboardInterrupt:
			process = None
		except (subprocess.CalledProcessError,OSError,ValueError):
			logger.worker('could not spawn process %s' % self.program, 'worker %d' % self.wid)
			process = None

		return process

	def _cleanup (self):
		logger.worker('terminating process', 'worker %d' % self.wid)
		# XXX: can raise
		self.response_box_read.close()
		self.response_box_write.close()
		try:
			if self.process:
				self.process.terminate()
				self.process.wait()
		except OSError, e:
			# No such processs
			if e[0] != errno.ESRCH:
				logger.worker('PID %s died' % pid, 'worker %d' % self.wid)

	def _resolveHost(self, host):
		# Do the hostname resolution before the backend check
		# We may block the page but filling the OS DNS cache can not harm :)
		# XXX: we really need an async dns .. sigh, another thread ?? 
		try:
			#raise socket.error('UNCOMMENT TO TEST DNS RESOLUTION FAILURE')
			return socket.gethostbyname(host)
		except socket.error,e:
			return None

	def stop (self):
		self.running = False
		self.request_box.put(None)

	def _classify (self,cid,client,method,url):
		squid = '%s %s - %s -' % (url,client,method)
		logger.worker('sending to classifier : [%s]' % squid, 'worker %d' % self.wid)
		try:
			self.process.stdin.write('%s%s' % (squid,os.linesep))
			self.process.stdin.flush()
			response = self.process.stdout.readline()

			logger.worker('received from classifier : [%s]' % response.strip(), 'worker %d' % self.wid)
			if response == '\n':
				response = host
			return response
		except IOError,e:
			logger.worker('IO/Error when sending to process, %s' % str(e), 'worker %d' % self.wid)
			self._reply(cid,500,'Interal Problem','could get a classification for %s' % url)
			# XXX: Do something
			return ''

	def _request (self,cid,ip,port,request):
		if regex.connection.match(request):
			request = re.sub('close',request)
		else:
			request = request.rstrip() + '\r\nConnection: Close\r\n\r\n'

		logger.worker('need to download data at %s' % str(ip), 'worker %d' % self.wid)
		self.response_box_write.write('%s %s %s %d %s\n' % (cid,'request',ip,port,request.replace('\n','\\n').replace('\r','\\r')))
		self.response_box_write.flush()
		##logger.worker('[%s %s %s %d %s]' % (cid,'request',ip,80,request), 'worker %d' % self.wid)
		self.last_worked = time.time()
	
	def _connect (self,cid,ip,port,request):
		self.response_box_write.write('%s %s %s %d %s\n' % (cid,'connect',ip,port,''))
		self.response_box_write.flush()

	def _reply (self,cid,code,title,body):
		self.response_box_write.write('%s %s %s %d %s\n' % (cid,'response',title.replace(' ','_'),code,body.replace('\n','\\n').replace('\r','\\r')))
		self.response_box_write.flush()

	def run (self):
		if not self.running:
			logger.worker('can not start', 'worker %d' % self.wid)
			return

		logger.worker('starting', 'worker %d' % self.wid)
		self.process = self._createProcess()
		if not self.process:
			# LOG SOMETHING !
			self.stop()

		while True:
			try:
				logger.worker('waiting for some work', 'worker %d' % self.wid)

				# XXX: For some reason, even if we have a timeout, pypy does block here
				data = self.request_box.get(1)

				# better to check here as we most likely will receive a stop during sleeping
				# as well we can get data as None and self.running still True !
				if not self.running or data is None:
					break

				cid,peer,request = data
				logger.worker('peer %s request %s' % (str(peer),' '.join(request.split('\n',3)[:2])), 'worker %d' % self.wid)
			except (ValueError, IndexError):
				logger.worker('received invalid message: %s' % data, 'worker %d' % self.wid)
				if not self.running:
					break
				continue
			except Empty:
				if not self.running:
					break
				continue

			method, url, host, client = self.parseRequest(request)
			if method is None:
				self._reply(cid, 400, 'INVALID REQUEST','invalid request <!--\nCDATA[[%s]]\n-->' % request)
				continue

			url_host = url.split('/')[2]
			if url_host.count(':'):
				url_port = url_host.split(':')[1]
				if url_port.isdigit():
					port = int(url_port)
				else:
					logger.worker('Could extract port from url %s' % url, 'worker %d' % self.wid)
					self._reply(cid,503,'INVALID URL','this is url is invalid %s' % url)
			else:
				port = 80

			ip = self._resolveHost(host)
			if not ip:
				logger.worker('Could not resolve %s' % host, 'worker %d' % self.wid)
				self._reply(cid,503,'NO DNS','could not resolve DNS for [%s]' % host)
				continue

			# classify and return the filtered page
			if method in ('GET','PUT','POST'):
				response = self._classify(cid,client,method,url)
				self._request(cid,ip,port,request)
				continue

			# someone want to use use as https proxy
			if method == 'CONNECT':
				self._connect(cid,ip,host,request)
				continue

			# do not bother classfying things which do not return pages
			if method in ('HEAD','OPTIONS','DELETE'):
				if False: # It should be an option to be able to force all request
					response = self._classify(cid,client,method,url)
				self._request(cid,ip,port,request)
				continue

			if method in ('TRACE',):
				self._reply(cid,501,'TRACE NOT IMPLEMENTED','This is bad .. we are sorry.')
				continue
		
		self._cleanup()
			# prevent persistence : http://tools.ietf.org/html/rfc2616#section-8.1.2.1
			# XXX: We may have more than one Connection header : http://tools.ietf.org/html/rfc2616#section-14.10
			# XXX: We may need to remove every step-by-step http://tools.ietf.org/html/rfc2616#section-13.5.1
			# XXX: We NEED to add a Via field http://tools.ietf.org/html/rfc2616#section-14.45
			# XXX: We NEED to respect Keep-Alive rules http://tools.ietf.org/html/rfc2068#section-19.7.1
			# XXX: We may look at Max-Forwards
			# XXX: We need to reply to "Proxy-Connection: keep-alive", with "Proxy-Connection: close"
			# http://homepage.ntlworld.com./jonathan.deboynepollard/FGA/web-proxy-connection-header.html