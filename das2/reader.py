"""Pure Python das2.2 and das2.3 stream reader.  Does not yet handle the 
das2.3/basic+xml type supported by the D reader yet.
"""

import sys
import os.path
from os.path import join as pjoin
from os.path import dirname as dname
from io import BytesIO

import xml.parsers.expat  # Switch das2C to use libxml2 as well?
from lxml import etree


class ReaderError(Exception):
	def __init__(self, line, message):
		self.line = line
		self.message = message
		super().__init__(self.message)

# ########################################################################### #

g_sDas2Stream = 'das-stream-v2.2.xsd'
g_sDas3BasicStream  = 'das-basic-stream-v3.0.xsd'
g_sDas3BasicDoc     = 'das-basic-doc-v3.0.xsd'

def getSchemaName(sStreamVer, sVariant):
	# If a fixed schema is given we have to load that
	if sStreamVer.startswith('2'): return g_sDas2Stream
	elif sStreamVer.startswith('3'):
		if sVariant == "das-basic-doc": return g_sDas3BasicDoc
		elif sVariant == "das-basic-stream": return g_sDas3BasicStream
	
	return None
	
def loadSchema(sStreamVer, sVariant):
	"""Load the appropriate das2 schema file from the package data location
	typcially this one of the files:

		$ROOT_DAS2_PKG/xsd/*.xsd

	Returns (schema, location):
		schema - An lxml.etree.XMLSchema object
		location - Where the schema was loaded from
	"""
	sMyDir = dname(os.path.abspath( __file__))
	sSchemaDir = pjoin(sMyDir, 'xsd')
	
	# If a fixed schema is given we have to load that
	sFile = getSchemaName(sStreamVer, sVariant)
	if not sFile:
		raise ValueError("Unknown stream version %s and variant %s"%(
			sStreamVer, sVariant
		))

	sPath = pjoin(sSchemaDir, sFile)	
	
	fSchema = open(sPath)
	schema_doc = etree.parse(fSchema)
	schema = etree.XMLSchema(schema_doc)
	
	return (schema,sPath)

# ########################################################################### #
def _getValSz(sType, nLine):
	"""das2 type names always end in the size, just count backwards and 
	pull off the digits.  You have to get at least one digit
	"""
	sSz = ""
	for c in reversed(sType):
		if c.isdigit(): sSz += c
		else: break
	
	if len(sSz) == 0:
		raise ReaderError(nLine, "Encoding length not defined in value '%s'"%sType)

	sSz = ''.join(reversed(sSz))
	return int(sSz, 10)

# ########################################################################### #

def _getDataLen(elPkt, sStreamVer, nPktId, bThrow=True):
	"""Given a <packet> element, recurse through top children and figure 
	out the data length.  Works for das2.2 and das2.3

	bThrow:  In general this should throw, but if we're doing validation
		There is no need to throw an exception for simple things that the
		schema checker will get anyway.
	"""
	nSize = 0

	# Das2.3 data can variable length array values in each packet.
	bArySep = ('arraysep' in elPkt.attrib)

	for child in elPkt:
		nItems = 1
		
		if sStreamVer == '2.2':
			# das2.2 had no extra XML elements in packet even in non-strict mode,
			# so everything should have a type attribute at this level
			if 'type' not in child.attrib:

				if bThrow:
					raise ValueError(
						"Attribute 'type' missing for element %s in packet ID %d"%(
						child.tag, nPktId
					))
				else:
					return None

			nSzEa = _getValSz(child.attrib['type'], child.sourceline)
		
			if child.tag == 'yscan':
				if 'nitems' in child.attrib:
					nItems = int(child.attrib['nitems'], 10)
			
			nSize += nSzEa * nItems
			
		elif sStreamVer == '2.3/basic':
		
			# das2.3 will allow extra elements at this level, so only look at
			# the stuff defined in the standard
			if child.tag not in ('x','y','z','w','yset','zset','wset'):
				continue
					
			if child.tag in ('yset','zset','wset'):
				if 'nitems' in child.attrib:
					lItems = [s.strip() for s in child.attrib['nitems'].split(',')]
					for sItem in lItems:

						# Allow for variable length number of items in das2.3 so
						# long as the packet has an array seperator defined.
						if sItem != "*":
							nItems *= int(sItem, 10)
					
			# Add sizes for all the planes, they all have the same number of items
			# but may have different value sizes
			for subChild in child:
				if subChild.tag == 'array':
			
					# Get the value type
					if 'encode' not in subChild.attrib:
						if bThrow:
							raise ReaderError(subChild.sourceline, 
								"Attribute 'encode' missing for element %s in packet ID %d"%(
								subChild.tag, nPktId
							))
						else:
							return None

				
					nSzEa = _getValSz(subChild.attrib['encode'], subChild.sourceline)
					nSize += nSzEa * nItems		
		
		else:
			raise ValueError("Unknown das2 stream version %s"%sStreamVer)
	
	return nSize

# ########################################################################### #

class Das22HdrParser:
	"""Deal with original das2's bad choices on properties elements.  Convert
	a single properties element into a container with sub elements so that
	it can be checked by schema documents.
	"""

	def __init__(self):
		self._builder = etree.TreeBuilder() # Save the parse tree here
		
		psr = xml.parsers.expat.ParserCreate('UTF-8') # Don't use namesapaces!
		psr.StartElementHandler  = self._elBeg
		psr.EndElementHandler    = self._elEnd
		psr.CharacterDataHandler = self._elData
		
		self._parser = psr
			
	def _elBeg(self, sName, dAttrs):
		# If we are beginning a properties element, then turn the attributes
		# into individual properties
		
		# Don't let the stream actually contain 'p' elements
		if sName == 'p':
			raise ValueError("Unknown element 'p' at line %d, column %d"%(
				self._parser.ErrorLineNumber, self._parser.ErrorColumnNumber
			))
				
		if sName != 'properties':
			el = self._builder.start(sName, dAttrs)
			el.sourceline = self._parser.CurrentLineNumber
			return el
		
		# Break out weird properity attributes into sub elements.  Fortunatly
		# lxml has a sourceline property we can set manually on elements since
		# we are creating them directly instead of the SAX parser.
		# (Thanks lxml!, Ya'll rock!)
		el = self._builder.start('properties', {})
		el.sourceline = self._parser.CurrentLineNumber
		
		for sKey in dAttrs:
			d = {'name':None}
			v = dAttrs[sKey]
			
			if ':' in sKey:
				l = [s.strip() for s in sKey.split(':')]
				
				if len(l) != 2 or (len(l[0]) == 0) or (len(l[1]) == 0):
					raise ValueError(
						"Malformed <property> attribute '%s' at line %d, column %d"%(
						sKey, self._parser.ErrorLineNumber, 
						self._parser.ErrorColumnNumber
					))
				
				d['name'] = l[1]
				if l[0] != 'String': # Strings are the default, drop the type
					d['type'] = l[0]
				
			else:
				d['name'] = sKey
			
			# Put the 'p' elements directly into the tree.  This keeps real
			# p elements from getting included, don't forget the sourceline
			el = self._builder.start('p', d)
			el.sourceline = self._parser.CurrentLineNumber
			self._builder.data(dAttrs[sKey].strip())
			self._builder.end('p')
		
	def _elData(self, sData):
		sData = sData.strip()		
		self._builder.data(sData)
	
	def _elEnd(self, sName):
		return self._builder.end(sName)
		
	def parse(self, fIn):
		if hasattr(fIn, 'read'):
			self._parser.ParseFile(fIn)
		else:
			self._parser.Parse(fIn, 1)
			
		elRoot = self._builder.close()
		return etree.ElementTree(elRoot)

# ########################################################################## #

class Packet(object):
	"""Represents a single packet from a das2 or qstream.

	Properties:
		sver - The version of the stream that produced the packet, should be
		   one of: 2.2, 2.3/basic or qstream

	   tag - The 2-character content tag, know header tags for das2 v3.0
		   streams are:
		     Hs - Stream Header
           Hi - I-slice dataset header
		     Cm - Comment
           Ex - Exception
           XX - Extra Packet, contents not defined
           Pi - I-slice data packet
			  
	   id - The packet integer ID.  Stream and pure dataset packets
		     are always ID 0.  Otherwise the ID is 1 or greater.
			
		length - The original length of the packet before decoding UTF-8
		     strings.
			
		content - Exther a bytestr (data packets) or a string (header 
		     packets.  If the packet is a header then the bytes are 
			  decode as utf-8. If the packet contains data the a raw
			  bytestr is returned.
	"""

	def __init__(self, sver, tag, id, length, content):
		self.sver    = sver
		self.tag     = tag
		self.id      = id
		self.length  = length
		self.content = content


class HdrPkt(Packet):

	def __init__(self, sver, tag, id, length, content):
		super(HdrPkt, self).__init__(sver, tag, id, length, content)
		self.tree    = None  # Cache the tree if one is created

	def docTree(self):
		"""Get an element tree from header packets
		
		Returns:
			An ElementTree object that may be used directly or run through
			a schema checker.

		Note, the parser ALTERS das2.2 headers to make them conform to standard
		XML conventions.  Namely the weird properties attributes are converted
		to sub elements.  For example this das2.2 properties input:

			<properties Datum:xTagWidth="128.000000 s"
		     double:zFill="-1.000000e+31" sourceId="das2_from_tagged_das1"
			/>

		Would be returned as if the following were read:

			<properties>
			  <p name="xTagWidth" type="Datum">128.000000 s</p>
			  <p name="zFill" type="double">-1.000000e+31</p>
			  <p name="sourceId">das2_from_tagged_das1</p>
			</properties>
		"""

		if not self.tree:

			fPkt = BytesIO(self.content)

			if self.sver == '2.2':
				parser = Das22HdrParser()
				self.tree = parser.parse(fPkt)
			else:
				self.tree = etree.parse(fPkt)

		return self.tree

class DataHdrPkt(HdrPkt):
	"""A header packet that describes data to be encountered later in the stream"""

	def __init__(self, sver, tag, id, length, content):
		super(DataHdrPkt, self).__init__(sver, tag, id, length, content)
		self.nDatLen = None

	def baseDataLen(self):
		"""The das2 parsable data length of each packet.  For das v3 streams
		extra information may reside in each packet after known das data.
		This function does not return the size of any extra items.
		"""
		
		if not self.nDatLen:
			tree = self.docTree()
			elRoot = tree.getroot()
			self.nDatLen = _getDataLen(elRoot, self.sver, self.id)
		
		return self.nDatLen

class DataPkt(Packet):
	"""A packet of data to display or otherwise use"""
	def __init__(self, sver, tag, id, length, content):
		super(DataPkt, self).__init__(sver, tag, id, length, content)

	# Nothing special defined for data packets yet


# ########################################################################## #

class PacketReader:
	"""This packet reader can handle either das v2.2 or v3.0 packets."""
	
	def __init__(self, fIn, bStrict=False):
		self.fIn = fIn
		self.lPktSize = [None]*100
		self.lPktDef  = [False]*100
		self.nOffset = 0
		self.bStrict = bStrict
		self.sContent = "das2"
		self.sVersion = "2.2"
		self.bVarTags = False
		
		# See if this stream is using variable tags and try to guess the content
		# using the first 80 bytes.  Assume a das2.2 stream unless we see
		# otherwise
		
		self.xFirst = fIn.read(80)
		
		if len(self.xFirst) > 0:
			if self.xFirst[0:1] == b'|':
				# Can't use single index for bytestring or it jumps over to an
				# integer return. Hence [0:1] instead of [0]. Yay python3 :(
				self.bVarTags = True
				
			if len(self.xFirst) > 3:
				if self.xFirst[0:4] == b'|Qs|':
					self.sContent = "qstream"
			
		if self.xFirst.find(b'version') != -1 and \
		   self.xFirst.find(b'"3.0"') != -1:
			self.sVersion = "3.0"
			
		elif self.xFirst.find(b'dataset_id') != -1:
			self.sContent = 'qstream'
	
	def streamType(self):
		return (self.sContent, self.sVersion, self.bVarTags)
		
		
	def _read(self, nBytes):
		xOut = b''
		if len(self.xFirst) > 0:
			xOut = self.xFirst[0:nBytes]
			self.xFirst = self.xFirst[nBytes:]
			
		if len(xOut) < nBytes:
			xOut += self.fIn.read(nBytes - len(xOut))

		return xOut

	def setDataSize(self, nPktId, nBytes):
		"""Callback used when parsing das2.2 and earlier streams.  These had
		no length values for the data packets.
		"""
		
		if nPktId < 1 or nPktId > 99:
			raise ValueError("Packet ID %d is invalid"%nPktid)
		if nBytes <= 0:
			raise ValueError("Data packet size %d is invalid"%nBytes)
		
		self.lPktSize[nPktId] = nBytes
		
	def __iter__(self):
		return self
		
	def next(self):
		return self.__next__()

		
	def __next__(self):
		"""Get the next packet on the stream. Each iteration returns a Packet
		object.  One of:

			Packet: For unknown items
			HdrPkt: For known header packets of a general nature
			DataHdrPkt: For known data header packets
			DataPkt: For known data containing packets
					
		The reader can iterate over all das2 streams, unless it has been
		set to strict mode
		"""
		x4 = self._read(4)
		if len(x4) != 4:
			raise StopIteration
					
		self.nOffset += 4
		
		# Try for a das v3 packet wrappers, fall back to v2.2 unless prevented
		if x4[0:1] == b'|':
			return self._nextVarTag(x4)
			
		elif (x4[0:1] == b'[') or (x4[0:1] == b':'):

			# In strict das2.3 mode, don't allow static tags
			if (self.sVersion != "2.2") and (self.bStrict):
				raise ValueError(
					"Das version 2 packet tag '%s' detected in a version 3 stream"%x4
				)

			return self._nextStaticTag(x4)
			
		raise ValueError(
			"Unknown packet tag character %s at offset %d, %s"%(
			str(x4[0:1]), self.nOffset - 4, 
			"(Hint: are the type lengths correct in the data header packet?)"
		))
	

	def _nextStaticTag(self, x4):
		"""Return a das2.2 packet, this is complicated by the fact that pre das3
		data packets don't have length value, parsing the associated header is required.
		"""
		
		try:
			nPktId = int(x4[1:3].decode('utf-8'), 10)
		except ValueError:
			raise ValueError("Invalid packet ID '%s'"%x4[1:3].decode('utf-8'))
			
		if (nPktId < 0) or (nPktId > 99):
			raise ValueError("Invalid packet ID %s at byte offset %s"%(
				x4[1:3].decode('utf-8'), self.nOffset
			))
			
		if self.nOffset == 4 and (x4 != b'[00]'):
			raise ValueError("Input does not start with '[00]' does not appear to be a das2 stream")
		
		if x4[0:1] == b'[' and x4[3:4] == b']':
		
			x6 = self._read(6)	
			if len(x6) != 6:
				raise ValueError("Premature end of packet %s"%x4.decode('utf-8'))
				
			self.nOffset += 6
			
			nLen = 0
			try:
				nLen = int(x6.decode('utf-8'), 10)
			except ValueError:
				raise ValueError("Invalid header length %s for packet %s"%(
					x6.decode('utf-8'), x4.decode('utf-8')
				))
				
			if nLen < 1:
				raise ValueError(
					"Packet length (%d) is to short for packet %s"%(
					nLen, x4.decode('utf-8')
				))
					
			xDoc = self._read(nLen)
			self.nOffset += nLen
			sDoc = None
			try:
				sDoc = xDoc.decode("utf-8")
			except UnicodeDecodeError:
				ValueError("Header %s (length %d bytes) is not valid UTF-8 text"%(
					x4.decode('utf-8'), nLen
				))
			
			self.lPktDef[nPktId] = True
			
			# Higher level parser will have to give us the length.  This is an
			# oversight in the das2 stream format that has been around for a while.
			# self.lPktSize = ? 
			
			# Also comment and exception packets are not differentiated, in das2.2
			# so we have to read ahead to get the content tag
			if x4 == b'[00]': 
				sTag = 'Hs'
				return HdrPkt(self.sVersion, sTag, nPktId, nLen, xDoc)

			elif nPktId > 0: 
				sTag = 'Hx'
				
				# Here's where das2.2 DROPPED THE BALL.  We have to know about
				# the higher level information just to get the size of a packet.
				# Every other networking protocol in the world knows to include
				# either lengths or terminators.  Geeeze.  Well, go parse it.
				parser = Das22HdrParser()
				fPkt = BytesIO(xDoc)
				docTree = parser.parse(fPkt)
				elRoot = docTree.getroot()
				self.lPktSize[nPktId] = _getDataLen(elRoot, self.sVersion, nPktId, False)

				return DataHdrPkt(self.sVersion, sTag, nPktId, nLen, xDoc)

			elif (x4 == b'[xx]') or (x4 == b'[XX]'):
				if sDoc.startswith('<exception'): sTag = 'He'
				elif sDoc.startswith('<comment'): sTag = 'Hc'
				elif sDoc.find('comment') > 1: sTag = 'Hc'
				elif sDoc.find('except') > 1: sTag = 'He'
				else: sTag = 'Hc'		

			return HdrPkt(self.sVersion, sTag, nPktId, nLen, xDoc)
		
		elif (x4[0:1] == b':') and  (x4[3:4] == b':'):
			# The old das2.2 packets which had no length, you had to parse the header.
			
			if not self.lPktDef[nPktId]:
				raise ValueError(
					"Undefined data packet %s encountered at offset %d"%(
					x4.decode('utf-8'), self.nOffset
				))
			
			if self.lPktSize[nPktId] == None:
				raise RuntimeError(
					"Internal error, unknown length for data packet %d"%nPktId
				)
			
			xData = self._read(self.lPktSize[nPktId])
			self.nOffset += len(xData)
			
			if len(xData) != self.lPktSize[nPktId]:
				raise ValueError("Premature end of packet data for id %d"%nPktId)
			
			return DataPkt(self.sVersion, 'Dx', nPktId, len(xData), xData)

		raise ValueError(
			"Expected the start of a header or data packet at offset %d"%self.nOffset
		)


	def _nextVarTag(self, x4):
		"""Return the next packet on the stream assuming das v3 packaging."""
				
		# Das3 uses '|' for field separators since they are not used by
		# almost any other language and won't be confused as xml elements or
		# json elements.
		
		nBegOffset = self.nOffset - 4
		
		# Accumulate the packet tag
		xTag = x4
		nPipes = 2
		while nPipes < 4:
			x1 = self._read(1)
			if len(x1) == 0: break
			self.nOffset += 1
			xTag += x1
			
			if x1 == b'|':
				nPipes += 1
			
			if len(xTag) > 38:
				raise ValueError(
					"Sanity limit of 38 bytes exceeded for packet tag '%s'"%(
						str(xTag)[2:-1])
				)
		
		try:
			lTag = [x.decode('utf-8') for x in xTag.split(b'|')[1:4] ]
		except UnicodeDecodeError:
			raise ValueError(
				"Packet tag '%s' is not utf-8 text at offset %d"%(xTag, nBegOffset)
			)
		
		sTag = lTag[0]
		nPktId = 0
		
		if len(lTag[1]) > 0:  # Empty packet IDs are the same as 0
			try:
				nPktId = int(lTag[1], 10)
			except ValueError:
				raise ValueError("Invalid packet ID '%s'"%lTag[1])
			
		if (nPktId < 0):
			raise ValueError("Invalid packet ID %d in tag at byte offset %d"%(
				nPktId, nBegOffset
			))
		
		try:
			nLen = int(lTag[2])
		except ValueError:
			raise ValueError(
				"Invalid length '%s' in packet tag at offset %d"%(lTag[2], nBegOffset)
			)
			
		if nLen < 2:
			raise ValueError(
				"Invalid packet length %d bytes at offset %d"%(nLen, nBegOffset)
			)
					
		xDoc = self._read(nLen)
		self.nOffset += len(xDoc)
			
		if len(xDoc) != nLen:
			raise ValueError("Pre-mature end of packet %s|%d at offset %d"%(
				sTag, nPktId, self.nOffset
			))
			
		if sTag not in ('Dx', 'Qd'):
			# In a header packet, insure it decodes to text
			sDoc = None
			try:
				sDoc = xDoc.decode("utf-8")
			except UnicodeDecodeError:
				ValueError("Header %s|%d (length %d bytes) is not valid UTF-8 text"%(
					sTag, nPktId, nLen
				))
			
			# Have to differentiate between general header packet and data 
			# header packet here
			if sTag == 'Hx':

				# Sanity check, make sure packet is big enough to hold minimum
				# size das3/basic data.
				fPkt = BytesIO(xDoc)
				docTree = etree.parse(fPkt)
				elRoot = docTree.getroot()
				self.lPktSize[nPktId] = _getDataLen(elRoot, self.sVersion, nPktId, False)

				return DataHdrPkt(self.sVersion, sTag, nPktId, nLen, xDoc)
			else:
				return HdrPkt(self.sVersion, sTag, nPktId, nLen, xDoc)
		else:
			# If this packet is below minimum necessary size fail it
			if nLen < self.lPktSize[nPktId]:
				raise ValueError(
					"Short data packet expected %d bytes found %d for |%s|%d| at offset %d"%(
					self.lPktSize[nPktId], nLen, sTag, nPktId, self.nOffset
				))
				
			# Even instrict mode, variable length packets are allowed, don't
			# complain about extra stuff in the packet
			#if self.bStrict and (nLen > self.lPktSize[nPktId]):
			#	raise ValueError("Strict checking requested, extra content "+\
			#	  "(%d bytes) not allowed for %s|%d at offset %d"%(
			#	  nLen - self.lPktSize[nPktId], sTag, nPktId, self.nOffset
			#	)) 

			# Return the bytes
			return DataPkt(self.sVersion, 'Dx', nPktId, nLen, xDoc)
