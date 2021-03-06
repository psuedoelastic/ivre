#! /usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of IVRE.
# Copyright 2011 - 2015 Pierre LALET <pierre.lalet@cea.fr>
#
# IVRE is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# IVRE is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
# License for more details.
#
# You should have received a copy of the GNU General Public License
# along with IVRE. If not, see <http://www.gnu.org/licenses/>.

"""This sub-module contains functions to interact with the MongoDB
databases.

"""

from ivre.db import DB, DBNmap, DBPassive, DBData, DBAgent
from ivre import utils, xmlnmap, config

import pymongo
import bson
import json

import re
import datetime


class MongoDB(DB):

    needunwind = []

    def __init__(self, host, dbname,
                 username=None, password=None, mechanism=None,
                 maxscan=None, maxtime=None,
                 **_):
        self.host = host
        self.dbname = dbname
        self.username = username
        self.password = password
        self.mechanism = mechanism
        try:
            self.maxscan = int(maxscan)
        except TypeError:
            self.maxscan = None
        try:
            self.maxtime = int(maxtime)
        except TypeError:
            self.maxtime = None
        self.indexes = {}
        self.specialindexes = {}
        self.hint_indexes = {}

    def set_limits(self, cur):
        if self.maxscan is not None:
            cur.max_scan(self.maxscan)
        if self.maxtime is not None:
            cur.max_time_ms(self.maxtime)
        return cur

    def get_hint(self, spec):
        """Given a query spec, return an appropriate index in a form
        suitable to be passed to Cursor.hint().

        """
        for fieldname, hint in self.hint_indexes.iteritems():
            if fieldname in spec:
                return hint

    @property
    def db(self):
        """The DB connection."""
        try:
            return self._db
        except AttributeError:
            self._db = pymongo.MongoClient(
                host=self.host,
                read_preference=pymongo.ReadPreference.SECONDARY_PREFERRED
            )[self.dbname]
            if self.username is not None:
                if self.password is not None:
                    self.db.authenticate(self.username, self.password)
                elif self.mechanism is not None:
                    self.db.authenticate(self.username,
                                         mechanism=self.mechanism)
                else:
                    raise TypeError("provide either 'password' or 'mechanism'"
                                    " with 'username'")
            return self._db

    @property
    def find(self):
        """Wrapper around column .find() method, depending on pymongo
        version.

        """
        try:
            return self._find
        except AttributeError:
            if pymongo.version_tuple[0] > 2:
                def _find(colname, *args, **kargs):
                    if 'spec' in kargs:
                        kargs['filter'] = kargs.pop('spec')
                    if 'fields' in kargs:
                        kargs['projection'] = kargs.pop('fields')
                    return self.db[colname].find(*args, **kargs)
                self._find = _find
            else:
                def _find(colname, *args, **kargs):
                    return self.db[colname].find(*args, **kargs)
                self._find = _find
            return self._find

    @property
    def find_one(self):
        """Wrapper around collection .find_one() method, depending on
        pymongo version.

        """
        try:
            return self._find_one
        except AttributeError:
            if pymongo.version_tuple[0] > 2:
                def _find_one(colname, *args, **kargs):
                    if 'spec_or_id' in kargs:
                        kargs['filter_or_id'] = kargs.pop('spec_or_id')
                    if 'fields' in kargs:
                        kargs['projection'] = kargs.pop('fields')
                    return self.db[colname].find_one(*args, **kargs)
                self._find_one = _find_one
            else:
                def _find_one(colname, *args, **kargs):
                    return self.db[colname].find_one(*args, **kargs)
                self._find_one = _find_one
            return self._find_one

    @staticmethod
    def getid(record):
        return record['_id']

    @staticmethod
    def serialize(obj):
        if type(obj) is bson.ObjectId:
            return obj.binary.encode('hex')
        return DB.serialize(obj)

    def explain(self, cursor, indent=None):
        return json.dumps(cursor.explain(), indent=indent,
                          default=self.serialize)

    @staticmethod
    def distinct(cursor, fieldname):
        return cursor.distinct(fieldname)

    def create_indexes(self):
        for colname, indexes in self.indexes.iteritems():
            for index in indexes:
                self.db[colname].create_index(index)
        for colname, indexes in self.specialindexes.iteritems():
            for index in indexes:
                self.db[colname].create_index(index[0], **index[1])

    def ensure_indexes(self):
        for colname, indexes in self.indexes.iteritems():
            for index in indexes:
                self.db[colname].ensure_index(index)
        for colname, indexes in self.specialindexes.iteritems():
            for index in indexes:
                self.db[colname].ensure_index(index[0], **index[1])

    def migrate_schema(self, colname, version):
        """Process to schema migrations in column `colname` starting
        from `version`.

        """
        while version in self.schema_migrations[colname]:
            new_version, migration_function = self.schema_migrations[
                colname][version]
            for record in self.set_limits(self.find(
                    colname, self.searchversion(version))):
                update = migration_function(record)
                if update is not None:
                    self.db[colname].update({"_id": record["_id"]}, update)
            version = new_version

    def cmp_schema_version(self, colname, document):
        """Returns 0 if the `document`'s schema version matches the
        code's current version for `colname`, -1 if it is higher (you
        need to update IVRE), and 1 if it is lower (you need to call
        .migrate_schema()).

        """
        return cmp(
            self.schema_latest_versions.get(colname, 0),
            document.get("schema_version", 0),
        )

    def _topvalues(self, field, flt=None, topnbr=10, sortby=None,
                   limit=None, skip=None, least=False, aggrflt=None,
                   specialproj=None, specialflt=None, countfield=None):
        """This method makes use of the aggregation framework to
        produce top values for a given field.

        """
        if flt is None:
            flt = self.flt_empty
        if aggrflt is None:
            aggrflt = self.flt_empty
        if specialflt is None:
            specialflt = []
        pipeline = []
        if flt:
            pipeline += [{"$match": flt}]
        if sortby is not None and ((limit is not None) or (skip is not None)):
            pipeline += [{"$sort": dict(sortby)}]
        if skip is not None:
            pipeline += [{"$skip": skip}]
        if limit is not None:
            pipeline += [{"$limit": limit}]
        if specialproj is None:
            project = {"_id": 0, field: 1}
            if countfield is not None:
                project[countfield] = 1
            pipeline += [{"$project": project}]
        else:
            pipeline += [{"$project": specialproj}]
        # hack to allow nested values as field
        # see <http://stackoverflow.com/questions/13708857/
        # mongodb-aggregation-framework-nested-arrays-subtract-expression>
        for i in xrange(field.count('.'), -1, -1):
            subfield = field.rsplit('.', i)[0]
            if subfield in self.needunwind:
                pipeline += [{"$unwind": "$" + subfield}]
        pipeline += specialflt
        # next step for previous hack
        project = {"field": "$%s" % field}
        if countfield is not None:
            project["count"] = "$%s" % countfield
        pipeline += [{"$project": project}]
        if aggrflt:
            pipeline += [{"$match": aggrflt}]
        else:
            # avoid null results
            pipeline += [{"$match": {"field": {"$exists": True}}}]
        pipeline += [{"$group": {"_id": "$field", "count": {
            "$sum": 1 if countfield is None else "$count"
        }}}]
        if least:
            pipeline += [{"$sort": {"count": 1}}]
        else:
            pipeline += [{"$sort": {"count": -1}}]
        if topnbr is not None:
            pipeline += [{"$limit": topnbr}]
        return pipeline

    # filters
    flt_empty = {}

    @staticmethod
    def str2id(string):
        return bson.ObjectId(string)

    @staticmethod
    def str2flt(string):
        return json.loads(string)

    @staticmethod
    def flt2str(flt):
        return json.dumps(flt)

    @staticmethod
    def _flt_and(cond1, cond2):
        """Returns a filter which will accept results if and only if
        they are accepted by both cond1 and cond2.

        """
        cond1k = set(cond1.keys())
        cond2k = set(cond2.keys())
        cond = {}
        if '$and' in cond1:
            cond1k.remove('$and')
            cond['$and'] = cond1['$and']
        if '$and' in cond2:
            cond2k.remove('$and')
            cond['$and'] = cond.get('$and', []) + cond2['$and']
        for k in cond1k.difference(cond2k):
            cond[k] = cond1[k]
        for k in cond2k.difference(cond1k):
            cond[k] = cond2[k]
        for k in cond1k.intersection(cond2k):
            if cond1[k] == cond2[k]:
                cond[k] = cond1[k]
            else:
                cond['$and'] = cond.get('$and', []) + [{k: cond1[k]},
                                                       {k: cond2[k]}]
        return cond

    @staticmethod
    def flt_or(*args):
        return {'$or': args} if len(args) > 1 else args[0]

    @staticmethod
    def searchid(idval, neg=False):
        """Filters (if `neg` == True, filters out) one particular
        record, given its id.

        """
        return {"_id": {'$ne': idval} if neg else idval}

    @staticmethod
    def searchversion(version):
        """Filters documents based on their schema's version."""
        return {"schema_version":
                {"$exists": False} if version is None else version}

    @staticmethod
    def searchhost(addr, neg=False):
        """Filters (if `neg` == True, filters out) one particular host
        (IP address).

        """
        try:
            addr = utils.ip2int(addr)
        except (TypeError, utils.socket.error):
            pass
        return {'addr': {'$ne': addr} if neg else addr}

    @staticmethod
    def searchhosts(hosts, neg=False):
        def convert(addr):
            try:
                return utils.ip2int(addr)
            except (TypeError, utils.socket.error):
                return addr
        return {'addr': {'$nin' if neg else '$in': [convert(host)
                                                    for host in hosts]}}

    @staticmethod
    def searchrange(start, stop, neg=False):
        """Filters (if `neg` == True, filters out) one particular IP
        address range.

        """
        try:
            start = utils.ip2int(start)
        except (TypeError, utils.socket.error):
            pass
        try:
            stop = utils.ip2int(stop)
        except (TypeError, utils.socket.error):
            pass
        if neg:
            return {'$or': [
                {'addr': {'$lt': start}},
                {'addr': {'$gt': stop}}
            ]}
        return {'addr': {'$gte': start,
                         '$lte': stop}}

    @staticmethod
    def searchval(key, val):
        return {key: val}

    @staticmethod
    def searchcmp(key, val, cmpop):
        if cmpop == '<':
            return {key: {'$lt': val}}
        elif cmpop == '<=':
            return {key: {'$lte': val}}
        elif cmpop == '>':
            return {key: {'$gt': val}}
        elif cmpop == '>=':
            return {key: {'$gte': val}}


class MongoDBNmap(MongoDB, DBNmap):

    content_handler = xmlnmap.Nmap2Mongo
    needunwind = ["categories", "ports", "ports.scripts",
                  "ports.scripts.ssh-hostkey", "scripts",
                  "ports.screenwords",
                  "scripts.smb-enum-shares.shares",
                  "extraports.filtered", "traces", "traces.hops",
                  "os.osmatch", "os.osclass", "hostnames",
                  "hostnames.domains", "cpes"]

    def __init__(self, host, dbname,
                 colname_scans="scans", colname_hosts="hosts",
                 colname_oldscans="archivesscans",
                 colname_oldhosts="archiveshosts",
                 **kargs):
        MongoDB.__init__(self, host, dbname, **kargs)
        DBNmap.__init__(self)
        self.colname_scans = colname_scans
        self.colname_hosts = colname_hosts
        self.colname_oldscans = colname_oldscans
        self.colname_oldhosts = colname_oldhosts
        self.indexes = {
            self.colname_hosts: [
                [('scanid', pymongo.ASCENDING)],
                [('schema_version', pymongo.ASCENDING)],
                [('addr', pymongo.ASCENDING)],
                [('starttime', pymongo.ASCENDING)],
                [('endtime', pymongo.ASCENDING)],
                [('source', pymongo.ASCENDING)],
                [('categories', pymongo.ASCENDING)],
                [('hostnames.domains', pymongo.ASCENDING)],
                [('traces.hops.domains', pymongo.ASCENDING)],
                [('openports.count', pymongo.ASCENDING)],
                [('openports.tcp.ports', pymongo.ASCENDING)],
                [('openports.udp.ports', pymongo.ASCENDING)],
                [('ports.port', pymongo.ASCENDING)],
                [('ports.state_state', pymongo.ASCENDING)],
                [('ports.service_name', pymongo.ASCENDING)],
                [('ports.scripts.id', pymongo.ASCENDING)],
                [('scripts.id', pymongo.ASCENDING)],
                [('infos.as_num', pymongo.ASCENDING)],
                [
                    ('traces.hops.ipaddr', pymongo.ASCENDING),
                    ('traces.hops.ttl', pymongo.ASCENDING),
                ],
                [
                    ('infos.country_code', pymongo.ASCENDING),
                    ('infos.city', pymongo.ASCENDING),
                ],
                [('infos.loc', pymongo.GEOSPHERE)],
            ],
            self.colname_oldhosts: [
                [('scanid', pymongo.ASCENDING)],
                [('schema_version', pymongo.ASCENDING)],
                [('addr', pymongo.ASCENDING)],
                [('starttime', pymongo.ASCENDING)],
                [('source', pymongo.ASCENDING)],
            ],
        }
        self.specialindexes = {
            self.colname_hosts: [
                ([('openports.tcp.count', pymongo.ASCENDING)],
                 {"sparse": True}),
                ([('openports.udp.count', pymongo.ASCENDING)],
                 {"sparse": True}),
                ([
                    ('ports.screenshot', pymongo.ASCENDING),
                    ('ports.screenwords', pymongo.ASCENDING),
                ],
                 {"sparse": True}),
                ([
                    ('cpes.type', pymongo.ASCENDING),
                    ('cpes.vendor', pymongo.ASCENDING),
                    ('cpes.product', pymongo.ASCENDING),
                    ('cpes.version', pymongo.ASCENDING),
                ],
                 {"sparse": True}),
            ],
            self.colname_oldhosts: [
                ([
                    ('ports.screenshot', pymongo.ASCENDING),
                    ('ports.screenwords', pymongo.ASCENDING),
                ],
                 {"sparse": True}),
            ],
        }
        self.schema_migrations = {
            self.colname_hosts: {
                None: (1, self.migrate_schema_hosts_0_1),
                1: (2, self.migrate_schema_hosts_1_2),
            },
        }
        self.schema_migrations[self.colname_oldhosts] = self.schema_migrations[
            self.colname_hosts].copy()
        self.schema_latest_versions = {
            self.colname_hosts: xmlnmap.SCHEMA_VERSION,
            self.colname_oldhosts: xmlnmap.SCHEMA_VERSION,
        }


    def init(self):
        """Initializes the "active" columns, i.e., drops those columns and
creates the default indexes."""
        self.db[self.colname_scans].drop()
        self.db[self.colname_hosts].drop()
        self.db[self.colname_oldscans].drop()
        self.db[self.colname_oldhosts].drop()
        self.create_indexes()

    def cmp_schema_version_host(self, host):
        """Returns 0 if the `host`'s schema version matches the code's
        current version, -1 if it is higher (you need to update IVRE),
        and 1 if it is lower (you need to call .migrate_schema()).

        """
        return self.cmp_schema_version(self.colname_hosts, host)

    def cmp_schema_version_scan(self, scan):
        """Returns 0 if the `scan`'s schema version matches the code's
        current version, -1 if it is higher (you need to update IVRE),
        and 1 if it is lower (you need to call .migrate_schema()).

        """
        return self.cmp_schema_version(self.colname_scans, scan)

    @staticmethod
    def migrate_schema_hosts_0_1(doc):
        """Converts a record from version 0 (no "schema_version" key
        in the document) to version 1 (`doc["schema_version"] ==
        1`). Version 1 adds an "openports" nested document to ease
        open ports based researches.

        """
        assert("schema_version" not in doc)
        assert("openports" not in doc)
        update = {"$set": {"schema_version": 1}}
        openports = {}
        for port in doc.get("ports", []):
            openports.setdefault(port["protocol"], {}).setdefault(
                "ports", []).append(port["port"])
        for proto in openports.keys():
            count = len(openports[proto]["ports"])
            openports[proto]["count"] = count
            openports["count"] = openports.get("count", 0) + count
        if not openports:
            openports["count"] = 0
        update["$set"]["openports"] = openports
        return update

    @staticmethod
    def migrate_schema_hosts_1_2(doc):
        """Converts a record from version 1 to version 2. Version 2
        discards service names when they have been found from
        nmap-services file.

        """
        assert(doc["schema_version"] == 1)
        update = {"$set": {"schema_version": 2}}
        update_ports = False
        for port in doc.get("ports", []):
            if port.get("service_method") == "table":
                update_ports = True
                for key in port.keys():
                    if key.startswith('service_'):
                        del port[key]
        if update_ports:
            update["$set"]["ports"] = doc["ports"]
        return update

    def get(self, flt, archive=False, **kargs):
        """Queries the active column (the old one if "archive" is set to True)
with the provided filter "flt", and returns a MongoDB cursor.

This should be very fast, as no operation is done (the cursor is only
returned). Next operations (e.g., .count(), enumeration, etc.) might
take a long time, depending on both the operations and the filter.

Any keyword argument other than "archive" is passed to the .find()
method of the Mongodb column object, without any validation (and might
have no effect if it is not expected)."""
        return self.set_limits(self.find(
            self.colname_oldhosts if archive else self.colname_hosts,
            flt,
            **kargs
        ))

    @staticmethod
    def getscanids(host):
        scanids = host.get('scanid')
        if scanids is None:
            return []
        if isinstance(scanids, list):
            return scanids
        return [scanids]

    def getscan(self, scanid, archive=False, **kargs):
        return self.find_one(
            self.colname_oldscans if archive else self.colname_scans,
            {'_id': scanid},
            **kargs
        )

    @staticmethod
    def getscreenshot(port):
        """Returns the content of a port's screenshot."""
        url = port.get('screenshot')
        if url is None:
            return None
        if url == "field":
            return port.get('screendata')

    def setscreenshot(self, host, port, data, protocol='tcp',
                      archives=False, overwrite=False):
        """Sets the content of a port's screenshot."""
        try:
            port = [p for p in host.get('ports', [])
                    if p['port'] == port and p['protocol'] == protocol][0]
        except IndexError:
            raise KeyError("Port %s/%d does not exist" % (protocol, port))
        if 'screenshot' in port and not overwrite:
            return
        port['screenshot'] = "field"
        port['screendata'] = bson.Binary(data)
        screenwords = utils.screenwords(data)
        if screenwords is not None:
            port['screenwords'] = screenwords
        self.db[
            self.colname_oldhosts if archives else self.colname_hosts
        ].update({"_id": host['_id']}, {"$set": {'ports': host['ports']}})

    def setscreenwords(self, host, port=None, protocol="tcp",
                       archives=False, overwrite=False):
        """Sets the `screenwords` attribute based on the screenshot
        data.

        """
        if port is None:
            if overwrite:
                flt_cond = lambda p: 'screenshot' in p
            else:
                flt_cond = lambda p: ('screenshot' in p
                                      and 'screenwords' not in p)
        else:
            if overwrite:
                flt_cond = lambda p: ('screenshot' in p
                                      and p.get('port') == port
                                      and p.get('protocol') == protocol)
            else:
                flt_cond = lambda p: ('screenshot' in p
                                      and 'screenwords' not in p
                                      and p.get('port') == port
                                      and p.get('protocol') == protocol)
        updated = False
        for port in host.get('ports', []):
            if not flt_cond(port):
                continue
            screenwords = utils.screenwords(self.getscreenshot(port))
            if screenwords is not None:
                port['screenwords'] = screenwords
                updated = True
        if updated:
            self.db[
                self.colname_oldhosts if archives else self.colname_hosts
            ].update({"_id": host['_id']}, {"$set": {'ports': host['ports']}})

    def removescreenshot(self, host, port=None, protocol='tcp',
                         archives=False):
        """Removes screenshots"""
        changed = False
        for p in host.get('ports', []):
            if port is None or (p['port'] == port and
                                p['protocol'] == protocol):
                if 'screenshot' in p:
                    if p['screenshot'] == "field":
                        if 'screendata' in p:
                            del p['screendata']
                    if 'screenwords' in p:
                        del p['screenwords']
                    del p['screenshot']
                    changed = True
        if changed:
            self.db[
                self.colname_oldhosts if archives else self.colname_hosts
            ].update({"_id": host["_id"]}, {"$set": {'ports': host['ports']}})

    def getlocations(self, flt, archive=False):
        col = self.db[self.colname_oldhosts if archive else self.colname_hosts]
        pipeline = [
            {"$match": self.flt_and(flt, self.searchhaslocation())},
            {"$project": {"_id": 0, "coords": "$infos.loc.coordinates"}},
            {"$group": {"_id": "$coords", "count": {"$sum": 1}}},
        ]
        return col.aggregate(pipeline, cursor={})

    def is_scan_present(self, scanid):
        for colname in [self.colname_scans, self.colname_oldscans]:
            if self.find_one(colname, {"_id": scanid},
                             fields=[]) is not None:
                return True
        return False

    def store_host(self, host):
        ident = self.db[self.colname_hosts].insert(host)
        print "HOST STORED: %r in %r" % (ident, self.colname_hosts)
        return ident

    def store_scan_doc(self, scan):
        ident = self.db[self.colname_scans].insert(scan)
        print "SCAN STORED: %r in %r" % (ident, self.colname_scans)
        return ident

    def merge_host_docs(self, rec1, rec2):
        """Merge two host records and return the result. Unmergeable /
        hard-to-merge fields are lost (e.g., extraports).

        """
        if rec1.get("schema_version") != rec2.get("schema_version"):
            raise ValueError("Cannot merge host documents. "
                             "Schema versions differ (%r != %r)" % (
                                 rec1.get("schema_version"),
                                 rec2.get("schema_version")))
        rec = {}
        if "schema_version" in rec1:
            rec["schema_version"] = rec1["schema_version"]
        # When we have different values, we will use the one from the
        # most recent scan, rec2
        if rec1.get("starttime") > rec2.get("starttime"):
            rec1, rec2 = rec2, rec1
        scanid = set()
        for record in [rec1, rec2]:
            scanid.update(self.getscanids(record))
        if scanid:
            if len(scanid) == 1:
                rec["scanid"] = scanid.pop()
            else:
                rec["scanid"] = list(scanid)
        for fname, function in [("starttime", min), ("endtime", max)]:
            try:
                rec[fname] = function(rec[fname] for rec in [rec1, rec2]
                                      if fname in rec)
            except ValueError:
                pass
        rec["state"] = ("up" if "up" in [rec.get("state")
                                         for rec in [rec1, rec2]]
                        else rec.get("state"))
        if rec["state"] is None:
            del rec["state"]
        rec["categories"] = list(
            set(rec1.get("categories", [])).union(
                rec2.get("categories", []))
        )
        for field in ["addr", "source", "os"]:
            rec[field] = rec2[field] if rec2.get(field) else rec1.get(field)
            if not rec[field]:
                del rec[field]
        rec["traces"] = rec1.get("traces", []) + rec2.get("traces", [])
        rec["infos"] = {}
        for record in [rec1, rec2]:
            rec["infos"].update(record.get("infos", {}))
        # We want to make sure of (type, name) unicity
        hostnames = dict(((h['type'], h['name']), h.get('domains'))
                         for h in (rec1.get("hostnames", [])
                                   + rec2.get("hostnames", [])))
        rec["hostnames"] = [{"type": h[0], "name": h[1], "domains": d}
                            for h, d in hostnames.iteritems()]
        scripts = dict((script["id"], script)
                       for script in rec1.get("scripts", []))
        scripts.update((script["id"], script)
                       for script in rec2.get("scripts", []))
        rec["scripts"] = scripts.values()
        ports = dict(((port["protocol"], port["port"]), port.copy())
                     for port in rec2.get("ports", []))
        for port in rec1.get("ports", []):
            if (port['protocol'], port['port']) in ports:
                curport = ports[(port['protocol'], port['port'])]
                if 'scripts' in curport:
                    curport['scripts'] = curport['scripts'][:]
                else:
                    curport['scripts'] = []
                present_scripts = set(
                    script['id'] for script in curport['scripts']
                )
                for script in port.get("scripts", []):
                    if script['id'] not in present_scripts:
                        curport['scripts'].append(script)
                if not curport['scripts']:
                    del curport['scripts']
                if port.get('service_method') == 'probed' and \
                   curport.get('service_method') != 'probed':
                    for key in port:
                        if key.startswith("service_"):
                            curport[key] = port[key]
            else:
                ports[(port['protocol'], port['port'])] = port
        rec["ports"] = ports.values()
        rec["openports"] = {}
        for record in [rec1, rec2]:
            for proto in record.get('openports', {}):
                if proto == 'count':
                    continue
                rec['openports'].setdefault(
                    proto, {}).setdefault(
                        'ports', set()).update(
                            record['openports'][proto]['ports'])
        if rec['openports']:
            for proto in rec['openports'].keys():
                count = len(rec['openports'][proto]['ports'])
                rec['openports'][proto]['count'] = count
                rec['openports']['count'] = rec['openports'].get(
                    'count', 0) + count
                rec['openports'][proto]['ports'] = list(
                    rec['openports'][proto]['ports'])
        else:
            rec['openports']["count"] = 0
        for field in ["traces", "infos", "scripts", "ports"]:
            if not rec[field]:
                del rec[field]
        return rec

    def remove(self, host, archive=False):
        """Removes the host "host" from the active (the old one if
        "archive" is set to True) column. "host" must be the host
        record as returned by MongoDB.

        If "host" has a "scanid" attribute, and if it refers to a scan
        that have no more host record after the deletion of "host",
        then the scan record is also removed.

        """
        if archive:
            colname_hosts = self.colname_oldhosts
            colname_scans = self.colname_oldscans
        else:
            colname_hosts = self.colname_hosts
            colname_scans = self.colname_scans
        self.db[colname_hosts].remove(spec_or_id=host['_id'])
        for scanid in self.getscanids(host):
            if self.get({'scanid': scanid}, archive=archive).count() == 0:
                self.db[colname_scans].remove(spec_or_id=scanid)

    def archive(self, host):
        """Archives a given host record. Also archives the
        corresponding scan and removes the scan from the "not
        archived" scan collection if not there is no host left in the
        "not archived" host collumn.

        """
        if self.find_one(self.colname_hosts, {"_id": host['_id']}) is None:
            if config.DEBUG:
                print("WARNING: cannot archive: host %s does not exist"
                      " in %r" % (host['_id'], self.colname_hosts))
        # store the host in the archive hosts collection
        self.db[self.colname_oldhosts].insert(host)
        if config.DEBUG:
            print "HOST ARCHIVED: %s in %r" % (
                host['_id'],
                self.colname_oldhosts,
            )
        # remove the host from the (not archived) hosts collection
        self.db[self.colname_hosts].remove(spec_or_id=host['_id'])
        if config.DEBUG:
            print "HOST REMOVED: %s from %r" % (
                host['_id'],
                self.colname_hosts,
            )
        for scanid in self.getscanids(host):
            scan = self.find_one(self.colname_scans, {'_id': scanid})
            if scan is not None:
                # store the scan in the archive scans collection if it
                # is not there yet
                if self.find_one(self.colname_oldscans,
                                 {'_id': scanid}) is None:
                    self.db[self.colname_oldscans].insert(scan)
                    if config.DEBUG:
                        print "SCAN ARCHIVED: %s in %r" % (
                            scanid,
                            self.colname_oldscans,
                        )
                # remove the scan from the (not archived) scans
                # collection if there is no more hosts related to this
                # scan in the hosts collection
                if self.find_one(self.colname_hosts,
                                 {'scanid': scanid}) is None:
                    self.db[self.colname_scans].remove(spec_or_id=scanid)
                    if config.DEBUG:
                        print "SCAN REMOVED: %s in %r" % (
                            scanid,
                            self.colname_scans,
                        )

    def archive_from_func(self, host, gettoarchive):
        if gettoarchive is None:
            return
        for rec in gettoarchive(host['addr'], host.get('source')):
            self.archive(rec)

    def get_mean_open_ports(self, flt, archive=False):
        """This method returns for a specific query `flt` a list of
        dictionary objects whose keys are `id` and `mean`; the value
        for `id` is a backend-dependant and uniquely identifies a
        record, and the value for `mean` is given by:

        (number of open ports) * sum(port number for each open port)

        This MongoDB specific implementation uses the aggregation
        framework to have most of the work done within the DB
        server.

        However, it is broken for now as it does not handle hosts with
        no open port but with a ports attribute.

        See
          * https://stackoverflow.com/questions/23636175
          * https://stackoverflow.com/questions/22114748
        """
        aggr = []
        if flt:
            aggr += [{"$match": flt}]
        aggr += [
            {"$project": {"ports.port": 1, "ports.state_state": 1}},
            # if the host has no ports attribute, we create an empty list
            {"$project": {"ports": {"$ifNull": ["$ports", []]}}},
            {"$redact": {"$cond": {"if": {"$eq": [{"$ifNull": ["$ports",
                                                               None]},
                                                  None]},
                                   "then": {
                                       "$cond": {"if": {"$eq": ["$state_state",
                                                                "open"]},
                                                 "then": "$$KEEP",
                                                 "else": "$$PRUNE"}},
                                   "else": "$$DESCEND"}}},
            {"$project": {"ports": {"$cond": [{"$eq": ["$ports", []]},
                                              [0],
                                              "$ports.port"]}}},
            {"$unwind": "$ports"},
            {"$group": {"_id": "$_id",
                        "count": {"$sum": 1},
                        "ports": {"$sum": "$ports"}}},
            {"$project": {"_id": 0,
                          "id": "$_id",
                          "mean": {"$multiply": ["$count", "$ports"]}}},
        ]
        return self.db[
            self.colname_oldhosts if archive
            else self.colname_hosts
        ].aggregate(aggr, cursor={})

    def group_by_port(self, flt, archive=False):
        """Work-in-progress function to get scan results grouped by
        common open ports

        """
        aggr = []
        if flt:
            aggr += [{"$match": flt}]
        aggr += [
            {"$project": {"ports.port": 1, "ports.state_state": 1}},
            # if the host has no ports attribute, we create an empty list
            {"$project": {"ports": {"$ifNull": ["$ports", []]}}},
            {"$redact": {"$cond": {"if": {"$eq": [{"$ifNull": ["$ports",
                                                               None]},
                                                  None]},
                                   "then": {
                                       "$cond": {"if": {"$eq": ["$state_state",
                                                                "open"]},
                                                 "then": "$$KEEP",
                                                 "else": "$$PRUNE"}},
                                   "else": "$$DESCEND"}}},
            {"$project": {"ports": {"$cond": [{"$eq": ["$ports", []]},
                                              [0],
                                              "$ports.port"]}}},
            {"$group": {"_id": "$ports",
                        "ids": {"$addToSet": "$_id"}}},
        ]
        return self.db[
            self.colname_oldhosts if archive
            else self.colname_hosts
        ].aggregate(aggr, cursor={})

    @staticmethod
    def json2dbrec(host):
        for fname in ["starttime", "endtime"]:
            if fname in host:
                host[fname] = datetime.datetime.strptime(
                    host[fname], "%Y-%m-%d %H:%M:%S"
                )
        for port in host.get('ports', []):
            if 'screendata' in port:
                port['screendata'] = bson.Binary(
                    port['screendata'].decode('base64')
                )
        return host

    @staticmethod
    def searchdomain(name, neg=False):
        if neg:
            if type(name) is utils.REGEXP_T:
                return {"hostnames.domains": {"$not": name}}
            return {"hostnames.domains": {"$ne": name}}
        return {"hostnames.domains": name}

    def searchhostname(self, name, neg=False):
        if neg:
            if type(name) is utils.REGEXP_T:
                return {"hostnames.name": {"$not": name}}
            return {"hostnames.name": {"$ne": name}}
        return self.flt_and(
            # This is indexed
            self.searchdomain(name, neg=neg),
            # This is not
            {"hostnames.name": name},
        )

    @staticmethod
    def searchcategory(cat, neg=False):
        """
        Filters (if `neg` == True, filters out) one particular category
        (records may have zero, one or more categories).
        """
        if neg:
            if type(cat) is utils.REGEXP_T:
                return {'categories': {'$not': cat}}
            return {'categories': {'$ne': cat}}
        return {'categories': cat}

    @staticmethod
    def searchcountry(country, neg=False):
        """Filters (if `neg` == True, filters out) one particular
        country, or a list of countries.

        """
        country = utils.country_unalias(country)
        if isinstance(country, list):
            return {'infos.country_code':
                    {'$nin' if neg else '$in': country}}
        return {'infos.country_code':
                {'$ne': country} if neg else country}

    @staticmethod
    def searchhaslocation(neg=False):
        return {'infos.loc': {"$exists": not neg}}

    @staticmethod
    def searchcity(city, neg=False):
        """
        Filters (if `neg` == True, filters out) one particular city.
        """
        if neg:
            if type(city) is utils.REGEXP_T:
                return {'infos.city': {'$not': city}}
            return {'infos.city': {'$ne': city}}
        return {'infos.city': city}

    @staticmethod
    def searchasnum(asnum, neg=False):
        """Filters (if `neg` == True, filters out) one or more
        particular AS number(s).

        """
        if type(asnum) not in [str, unicode] and hasattr(asnum, '__iter__'):
            return {'infos.as_num':
                    {'$nin' if neg else '$in': map(int, asnum)}}
        asnum = int(asnum)
        return {'infos.as_num': {'$ne': asnum} if neg else asnum}

    @staticmethod
    def searchasname(asname, neg=False):
        """Filters (if `neg` == True, filters out) one or more
        particular AS.

        """
        if neg:
            if type(asname) is utils.REGEXP_T:
                return {'infos.as_name': {'$not': asname}}
            else:
                return {'infos.as_name': {'$ne': asname}}
        return {'infos.as_name': asname}

    @staticmethod
    def searchsource(src, neg=False):
        """Filters (if `neg` == True, filters out) one particular
        source.

        """
        if neg:
            if type(src) is utils.REGEXP_T:
                return {'source': {'$not': src}}
            return {'source': {'$ne': src}}
        return {'source': src}

    @staticmethod
    def searchport(port, protocol='tcp', state='open', neg=False):
        """Filters (if `neg` == True, filters out) records with
        specified protocol/port at required state. Be aware that when
        a host has a lot of ports filtered or closed, it will not
        report all of them, but only a summary, and thus the filter
        might not work as expected. This filter will always work to
        find open ports.

        """
        if state == "open":
            return {"openports.%s.ports" % protocol:
                    {'$ne': port} if neg else port}
        if neg:
            return {
                '$or': [
                    {'ports': {'$elemMatch': {
                        'port': port,
                        'protocol': protocol,
                        'state_state': {'$ne': state}
                    }}},
                    {'ports.port': {'$ne': port}},
                ]
            }
        return {'ports': {'$elemMatch': {
            'port': port,
            'protocol': protocol,
            'state_state': state
        }}}

    def searchportsother(self, ports, protocol='tcp', state='open'):
        """Filters records with at least one port other than those
        listed in `ports` with state `state`.

        """
        return self.searchport(
            {'$elemMatch': {'$nin': ports}} if state == 'open'
            else {'$nin': ports},
            protocol=protocol,
            state=state,
        )

    def searchports(self, ports, protocol='tcp', state='open', neg=False):
        if state == "open" and not neg:
            return self.searchport({'$all': ports}, state=state,
                                   protocol=protocol, neg=neg)
        if neg:
            return self.flt_and(*(self.searchport(p, protocol=protocol,
                                                  state=state, neg=neg)
                                  for p in ports))
        return {'ports': {'$all': [
            self.searchport(port, protocol=protocol,
                            state=state, neg=neg)['ports']
            for port in ports]}}

    @staticmethod
    def searchobjectid(oid, neg=False):
        """Filters records by their ObjectID.
        `oid` can be a single or many (as a list or any iterable) object ID(s),
        specified as strings or an `ObjectID`s."""
        if isinstance(oid, (basestring, bson.objectid.ObjectId)):
            oid = [bson.objectid.ObjectId(oid)]
        else:
            oid = [bson.objectid.ObjectId(elt) for elt in oid]
        if len(oid) == 1:
            return {'_id': {'$ne': oid[0]} if neg else oid[0]}
        return {'_id': {'$nin' if neg else '$in': oid}}

    @staticmethod
    def searchcountopenports(minn=None, maxn=None, neg=False):
        "Filters records with open port number between minn and maxn"
        assert(minn is not None or maxn is not None)
        flt = []
        if minn == maxn:
            return {'openports.count': {'$ne': minn} if neg else minn}
        if minn is not None:
            flt.append({'$lt' if neg else '$gte': minn})
        if maxn is not None:
            flt.append({'$gt' if neg else '$lte': maxn})
        if len(flt) == 1:
            return {'openports.count': flt[0]}
        if neg:
            return {'$or': [{'openports.count': cond} for cond in flt]}
        # return {'openports.count':
        #         dict(item for cond in flt for item in cond.iteritems())}
        return {'openports.count': {'$lte':maxn, '$gte':minn}}

    @staticmethod
    def searchopenport(neg=False):
        "Filters records with at least one open port."
        return {'ports.state_state': {'$nin': ['open']} if neg else 'open'}

    @staticmethod
    def searchservice(srv, port=None, protocol=None):
        """Search an open port with a particular service."""
        flt = {'service_name': srv}
        if port is not None:
            flt['port'] = port
        if protocol is not None:
            flt['protocol'] = protocol
        if len(flt) == 1:
            return {'ports.service_name': srv}
        return {'ports': {'$elemMatch': flt}}

    @staticmethod
    def searchproduct(product, version=None, service=None, port=None,
                      protocol=None):
        """Search a port with a particular `product`. It is (much)
        better to provide the `service` name and/or `port` number
        since those fields are indexed.

        """
        flt = {'service_product': product}
        if version is not None:
            flt['service_version'] = version
        if service is not None:
            flt['service_name'] = service
        if port is not None:
            flt['port'] = port
        if protocol is not None:
            flt['protocol'] = protocol
        if len(flt) == 1:
            return {'ports.service_product': product}
        return {'ports': {'$elemMatch': flt}}

    @staticmethod
    def searchscript(host=False, name=None, output=None, values=None):
        """Search a particular content in the scripts results.

        """
        key = "scripts" if host else "ports.scripts"
        req = {}
        if name is not None:
            req['id'] = name
        if output is not None:
            req['output'] = output
        if values is not None:
            if name is None:
                raise TypeError(".searchscript() needs a `name` arg "
                                "when using a `values` arg")
            for field, value in values.iteritems():
                req["%s.%s" % (name, field)] = value
        if not req:
            return {key: {"$exists": True}}
        if len(req) == 1:
            field, value = req.items()[0]
            return {"%s.%s" % (key, field): value}
        return {key: {"$elemMatch": req}}

    def searchscriptidout(self, name, output):
        """DEPRECATED: use .searchscript() instead.

        Search a particular content in the scripts names and
        outputs.

        """
        return self.searchscript(name=name, output=output)

    def searchscriptid(self, name):
        """DEPRECATED: use .searchscript() instead.

        Search a script name.

        """
        return self.searchscript(name=name)

    def searchscriptoutput(self, expr):
        """DEPRECATED: use .searchscript() instead.

        Search a particular content in the scripts outputs.

        """
        return self.searchscript(output=expr)

    @staticmethod
    def searchsvchostname(srv):
        return {'ports.service_hostname': srv}

    @staticmethod
    def searchwebmin():
        return {
            'ports': {
                '$elemMatch': {
                    'service_name': 'http',
                    'service_product': 'MiniServ',
                    'service_extrainfo': {'$ne': 'Webmin httpd'},
                }}}

    @staticmethod
    def searchx11():
        return {
            'ports': {'$elemMatch': {
                'service_name': 'X11',
                'service_extrainfo': {'$ne': 'access denied'}
            }}}

    @staticmethod
    def searchsmb(**args):
        # key aliases
        if 'dnsdomain' in args:
            args['domain_dns'] = args.pop('dnsdomain')
        if 'forest' in args:
            args['forest_dns'] = args.pop('forest')
        # Build the query. Do *not* iterate here since we are
        # modifying the dictionary
        for key in args.keys():
            args['smb-os-discovery.%s' % key] = args.pop(key)
        args['id'] = 'smb-os-discovery'
        return {
            'scripts': {'$elemMatch': args}
        }

    def searchfile(self, fname):
        if type(fname) is not utils.REGEXP_T:
            fname = re.compile(re.escape(fname))
        return self.searchscript(
            name={'$in': ['ftp-anon', 'afp-ls', 'gopher-ls',
                          'http-vlcstreamer-ls', 'nfs-ls', 'smb-ls']},
            output=fname,
        )

    def searchsmbshares(self, access='', hidden=None):
        """Filter SMB shares with given `access` (default: either read
        or write, accepted values 'r', 'w', 'rw').

        If `hidden` is set to `True`, look for hidden shares, for
        non-hidden if set to `False` and for both if set to `None`
        (this is the default).

        """
        access = {
            '': re.compile('^(READ|WRITE)'),
            'r': re.compile('^READ(/|$)'),
            'w': re.compile('(^|/)WRITE$'),
            'rw': 'READ/WRITE',
            'wr': 'READ/WRITE',
        }[access.lower()]
        share_type = {
            # None: re.compile('^STYPE_DISKTREE(_HIDDEN)?$'),
            # None: accept share in unsure
            None: {'$nin': ['STYPE_IPC_HIDDEN', 'Not a file share',
                            'STYPE_IPC', 'STYPE_PRINTQ']},
            True: 'STYPE_DISKTREE_HIDDEN',
            False: 'STYPE_DISKTREE',
        }[hidden]
        return self.searchscript(
            name='smb-enum-shares',
            values={'shares': {'$elemMatch': {
                '$or': [
                    {'%s access' % user: access} for user in ['Anonymous',
                                                              'Current user']
                ],
                'Type': share_type,
                'Share': {'$ne': 'IPC$'},
            }}},
            host=True)

    def searchhttptitle(self, title):
        return self.searchscript(
            name={'$in': ['http-title', 'html-title']},
            output=title,
        )

    @staticmethod
    def searchservicescript(srv, port=None):
        if port is None:
            return {
                'ports': {
                    '$elemMatch': {
                        'state_state': 'open',
                        '$or': [
                            {'service_name': srv},
                            {'service_product': srv},
                            {'service_version': srv},
                            {'service_extrainfo': srv},
                            {'service_hostname': srv},
                            {'scripts.id': srv},
                            {'scripts.output': srv}
                        ]
                    }}}
        return {
            'ports': {
                '$elemMatch': {
                    'state_state': 'open',
                    'port': port,
                    '$or': [
                        {'service_name': srv},
                        {'service_product': srv},
                        {'service_version': srv},
                        {'service_extrainfo': srv},
                        {'service_hostname': srv},
                        {'scripts.id': srv},
                        {'scripts.output': srv}
                    ]
                }}}

    def searchhostscript(self, txt):
        """DEPRECATED: use .searchscript() instead."""
        return self.searchscript(host=True, output=txt)

    def searchhostscriptid(self, name):
        """DEPRECATED: use .searchscript() instead."""
        return self.searchscript(host=True, name=name)

    def searchhostscriptidout(self, name, out):
        """DEPRECATED: use .searchscript() instead."""
        return self.searchscript(host=True, name=name, output=out)

    @staticmethod
    def searchos(txt):
        return {
            '$or': [
                {'os.osclass.vendor': txt},
                {'os.osclass.osfamily': txt},
                {'os.osclass.osgen': txt}
            ]}

    @staticmethod
    def searchvsftpdbackdoor():
        return {
            'ports': {
                '$elemMatch': {
                    'protocol': 'tcp',
                    'state_state': 'open',
                    'service_product': 'vsftpd',
                    'service_version': '2.3.4',
                }}}

    @staticmethod
    def searchvulnintersil():
        # See MSF modules/auxiliary/admin/http/intersil_pass_reset.rb
        return {
            'ports': {
                '$elemMatch': {
                    'protocol': 'tcp',
                    'state_state': 'open',
                    'service_product': 'Boa HTTPd',
                    'service_version': re.compile('^0\\.9(3([^0-9]|$)|'
                                                  '4\\.([0-9]|0[0-9]|'
                                                  '1[0-1])([^0-9]|$))')
                }}}

    @staticmethod
    def searchdevicetype(devtype):
        return {'ports.service_devicetype': devtype}

    def searchnetdev(self):
        return self.searchdevicetype({
            '$in': [
                'bridge',
                'broadband router',
                'firewall',
                'hub',
                'load balancer',
                'proxy server',
                'router',
                'switch',
                'WAP',
            ]})

    def searchphonedev(self):
        return self.searchdevicetype({
            '$in': [
                'PBX',
                'phone',
                'telecom-misc',
                'VoIP adapter',
                'VoIP phone',
            ]})

    @staticmethod
    def searchldapanon():
        return {'ports.service_extrainfo': 'Anonymous bind OK'}

    @staticmethod
    def searchtimeago(delta, neg=False):
        if not isinstance(delta, datetime.timedelta):
            delta = datetime.timedelta(seconds=delta)
        return {'endtime': {'$lt' if neg else '$gte':
                            datetime.datetime.now() - delta}}

    def searchtimerange(self, start, stop, neg=False):
        if not isinstance(start, datetime.datetime):
            start = datetime.datetime.fromtimestamp(start)
        if not isinstance(stop, datetime.datetime):
            stop = datetime.datetime.fromtimestamp(stop)
        if neg:
            return self.flt_or(
                {'endtime': {'$lt': start}},
                {'starttime': {'$gt': stop}}
            )
        return {'endtime': {'$gte': start}, 'starttime': {'$lte': stop}}

    @staticmethod
    def searchhop(hop, ttl=None, neg=False):
        try:
            hop = utils.ip2int(hop)
        except (TypeError, utils.socket.error):
            pass
        if ttl is None:
            return {'traces.hops.ipaddr': {'$ne': hop} if neg else hop}
        if neg:
            return {
                '$or': [
                    {'traces.hops': {'$elemMatch': {
                        'ttl': ttl,
                        'ipaddr': {'$ne': hop},
                    }}},
                    {'traces.hops.ttl': {'$ne': ttl}},
                ]
            }
        return {'traces.hops': {'$elemMatch': {'ipaddr': hop, 'ttl': ttl}}}

    @staticmethod
    def searchhopdomain(hop, neg=False):
        if neg:
            if type(hop) is utils.REGEXP_T:
                return {'traces.hops.domains': {'$not': hop}}
            return {'traces.hops.domains': {'$ne': hop}}
        return {'traces.hops.domains': hop}

    def searchhopname(self, hop, neg=False):
        if neg:
            if type(hop) is utils.REGEXP_T:
                return {'traces.hops.host': {'$not': hop}}
            return {'traces.hops.host': {'$ne': hop}}
        return self.flt_and(
            # This is indexed
            self.searchhopdomain(hop, neg=neg),
            # This is not
            {'traces.hops.host': hop},
        )

    @staticmethod
    def searchscreenshot(port=None, protocol='tcp', service=None, words=None,
                         neg=False):
        """Filter results with (without, when `neg == True`) a
        screenshot (on a specific `port` if specified).

        `words` can be specified as a string, a regular expression, a
        boolean, or as a list and is/are matched against the OCR
        results. When `words` is specified and `neg == True`, the
        result will filter results **with** a screenshot **without**
        the word(s) in the OCR results.

        """
        result = {'ports': {'$elemMatch': {}}}
        if words is None:
            if port is None and service is None:
                return {'ports.screenshot': {'$exists': not neg}}
            result['ports']['$elemMatch']['screenshot'] = {'$exists': not neg}
        else:
            result['ports']['$elemMatch']['screenshot'] = {'$exists': True}
            if isinstance(words, list):
                words = {'$ne' if neg else '$all': words}
            elif type(words) is utils.REGEXP_T:
                words = {'$not': words} if neg else words
            elif type(words) is bool:
                words = {"$exists": words}
            else:
                words = {'$ne': words} if neg else words
            result['ports']['$elemMatch']['screenwords'] = words
        if port is not None:
            result['ports']['$elemMatch']['port'] = port
            result['ports']['$elemMatch']['protocol'] = protocol
        if service is not None:
            result['ports']['$elemMatch']['service_name'] = service
        return result

    @staticmethod
    def searchcpe(cpe_type=None, vendor=None, product=None, version=None):
        """Look for a CPE by type (a, o or h), vendor, product or version (the
        part after the column following the product). No argument will just
        check for cpe existence.

        """
        fields = [
            ("type", cpe_type),
            ("vendor", vendor),
            ("product", product),
            ("version", version),
        ]
        flt = dict((field, value)
                   for field, value in fields
                   if value is not None)
        nflt = len(flt)
        if nflt == 0:
            return {"cpes": {"$exists": True}}
        elif nflt == 1:
            field, value = flt.popitem()
            return {"cpes.%s" % field: value}
        else:
            return {"cpes": {"$elemMatch": flt}}

    def topvalues(self, field, flt=None, topnbr=10, sortby=None,
                  limit=None, skip=None, least=False, archive=False,
                  aggrflt=None, specialproj=None, specialflt=None):
        """
        This method makes use of the aggregation framework to produce
        top values for a given field or pseudo-field. Pseudo-fields are:
          - category / asnum / country
          - port
          - port:open / :closed / :filtered / :<servicename>
          - portlist:open / :closed / :filtered
          - countports:open / :closed / :filtered
          - service / service:<portnbr>
          - product / product:<portnbr>
          - cpe / cpe.<part> / cpe:<cpe_spec> / cpe.<part>:<cpe_spec>
          - devicetype / devicetype:<portnbr>
          - [port]script:<scriptid> / hostscript:<scriptid>
          - cert.* / smb.* / sshkey.*
          - modbus.* / s7.* / enip.*
          - screenwords
          - hop
        """
        colname = self.colname_oldhosts if archive else self.colname_hosts
        outputproc = None
        if flt is None:
            flt = self.flt_empty
        if aggrflt is None:
            aggrflt = self.flt_empty
        if specialflt is None:
            specialflt = []
        # pseudo-fields
        if field == "category":
            field = "categories"
        elif field == "country":
            flt = self.flt_and(flt, {"infos.country_code": {"$exists": True}})
            field = "infos.country_code"
            outputproc = lambda x: {
                'count': x['count'],
                '_id': [
                    x['_id'],
                    self.globaldb.data.country_name_by_code(x['_id']),
                ]}
        elif field == "city":
            flt = self.flt_and(
                flt,
                {"infos.country_code": {"$exists": True}},
                {"infos.city": {"$exists": True}}
            )
            specialproj = {"_id": 0,
                           "city": {"$concat": [
                               "$infos.country_code",
                               "###",
                               "$infos.city",
                           ]}}
            field = "city"
            outputproc = lambda x: {'count': x['count'],
                                    '_id': x['_id'].split('###')}
        elif field == "asnum":
            flt = self.flt_and(flt, {"infos.as_num": {"$exists": True}})
            field = "infos.as_num"
        elif field == "as":
            flt = self.flt_and(flt, {"infos.as_num": {"$exists": True}})
            specialproj = {
                "_id": 0,
                "as": {"$concat": [
                    # hack to convert the integer to a string and
                    # prevent the exception "$concat only supports
                    # strings, not NumberInt32"
                    {"$toLower": "$infos.as_num"},
                    "###",
                    "$infos.as_name",
                ]}}
            field = "as"
            outputproc = lambda x: {'count': x['count'],
                                    '_id': x['_id'].split('###')}
        elif field == "port":
            field = "ports.port"
        elif field.startswith("port:"):
            info = field.split(':', 1)[1]
            flt_field = "ports.%s" % (
                "state_state"
                if info in ['open', 'filtered', 'closed'] else
                "service_name"
            )
            field = "ports.port"
            flt = self.flt_and(flt, {flt_field: info})
            specialproj = {"_id": 0, field: 1, flt_field: 1}
            specialflt = [
                {"$match": {flt_field: info}},
                {"$project": {field: 1}}
            ]
        elif field.startswith("portlist:"):
            specialproj = {"ports.port": 1, "ports.state_state": 1}
            specialflt = [
                {"$project": {"ports.port": 1, "ports.state_state": 1}},
                # if the host has no ports attribute, we create an empty list
                {"$project": {"ports": {"$ifNull": ["$ports", []]}}},
                # We use $redact instead of $match to keep an empty
                # list when no port matches.
                #
                # The firts "$cond" help us make the difference
                # between main document ($ports exists in that case)
                # and a nested document ($ports does not exist in that
                # case). The second only keeps ports we are interested in.
                {"$redact": {"$cond": {"if": {"$eq": [{"$ifNull": ["$ports",
                                                                   None]},
                                                      None]},
                                       "then": {
                                           "$cond": {
                                               "if": {"$eq": [
                                                   "$state_state",
                                                   field.split(':', 1)[1]]},
                                               "then": "$$KEEP",
                                               "else": "$$PRUNE"}},
                                       "else": "$$DESCEND"}}},
                {"$project": {"portlist": "$ports.port"}},
            ]
            field = "portlist"
        elif field.startswith('countports:'):
            state = field.split(':', 1)[1]
            if state == 'open':
                field = "openports.count"
            else:
                specialproj = {"_id": 0,
                               "ports.state_state": 1}
                specialflt = [
                    {"$project": {"ports": {"$ifNull": ["$ports", []]}}},
                    # See "portlist:".
                    {"$redact": {"$cond": {"if": {"$eq": [{"$ifNull": ["$ports",
                                                                       None]},
                                                          None]},
                                           "then": {
                                               "$cond": {
                                                   "if": {"$eq": [
                                                       "$state_state",
                                                       state]},
                                                   "then": "$$KEEP",
                                                   "else": "$$PRUNE"}},
                                           "else": "$$DESCEND"}}},
                    {"$project": {"countports": {"$size": "$ports"}}},
                ]
                field = "countports"
        elif field == "service":
            flt = self.flt_and(flt, self.searchservice({'$exists': True}))
            field = "ports.service_name"
        elif field.startswith("service:"):
            port = int(field.split(':', 1)[1])
            flt = self.flt_and(
                flt,
                self.searchservice({'$exists': True}, port=port),
            )
            specialproj = {"_id": 0, "ports.port": 1, "ports.service_name": 1}
            specialflt = [
                {"$match": {"ports.port": port}},
                {"$project": {"ports.service_name": 1}}
            ]
            field = "ports.service_name"
        elif field == 'product':
            flt = self.flt_and(flt, self.searchproduct(
                {'$exists': True},
                service={'$exists': True},
            ))
            specialproj = {
                "_id": 0,
                "ports.service_name": 1,
                "ports.service_product": 1,
            }
            specialflt = [
                {"$match": {"ports.service_product": {"$exists": True}}},
                {"$project":
                 {"ports.service_product":
                  {"$concat": [
                      "$ports.service_name",
                      "###",
                      "$ports.service_product",
                  ]}}}
            ]
            field = "ports.service_product"
            outputproc = lambda x: {'count': x['count'],
                                    '_id': x['_id'].split('###')}
        elif field.startswith('product:'):
            port = int(field.split(':', 1)[1])
            flt = self.flt_and(flt, self.searchproduct(
                {'$exists': True},
                service={'$exists': True},
                port=port,
            ))
            specialproj = {
                "_id": 0,
                "ports.port": 1,
                "ports.service_name": 1,
                "ports.service_product": 1,
            }
            specialflt = [
                {"$match": {"ports.port": port,
                            "ports.service_product": {"$exists": True}}},
                {"$project":
                 {"ports.service_product":
                  {"$concat": [
                      "$ports.service_name",
                      "###",
                      "$ports.service_product",
                  ]}}}
            ]
            field = "ports.service_product"
            outputproc = lambda x: {'count': x['count'],
                                    '_id': x['_id'].split('###')}
        elif field == 'version':
            flt = self.flt_and(flt, self.searchproduct(
                {'$exists': True},
                service={'$exists': True},
                version={'$exists': True},
            ))
            specialproj = {
                "_id": 0,
                "ports.service_name": 1,
                "ports.service_product": 1,
                "ports.service_version": 1,
            }
            specialflt = [
                {"$match": {"ports.service_product": {"$exists": True},
                            "ports.service_version": {"$exists": True}}},
                {"$project":
                 {"ports.service_product":
                  {"$concat": [
                      "$ports.service_name",
                      "###",
                      "$ports.service_product",
                      "###",
                      "$ports.service_version",
                  ]}}}
            ]
            field = "ports.service_product"
            outputproc = lambda x: {'count': x['count'],
                                    '_id': x['_id'].split('###')}
        elif field.startswith('version:'):
            port = int(field.split(':', 1)[1])
            flt = self.flt_and(flt, self.searchproduct(
                {'$exists': True},
                service={'$exists': True},
                version={'$exists': True},
                port=port
            ))
            specialproj = {
                "_id": 0,
                "ports.port": 1,
                "ports.service_name": 1,
                "ports.service_product": 1,
                "ports.service_version": 1,
            }
            specialflt = [
                {"$match": {"ports.port": port,
                            "ports.service_product": {"$exists": True},
                            "ports.service_version": {"$exists": True}}},
                {"$project":
                 {"ports.service_product":
                  {"$concat": [
                      "$ports.service_name",
                      "###",
                      "$ports.service_product",
                      "###",
                      "$ports.service_version",
                  ]}}}
            ]
            field = "ports.service_product"
            outputproc = lambda x: {'count': x['count'],
                                    '_id': x['_id'].split('###')}
        elif field.startswith("cpe"):
            try:
                field, cpeflt = field.split(":", 1)
                cpeflt = cpeflt.split(':', 3)
            except ValueError:
                cpeflt = []
            try:
                field = field.split(".", 1)[1]
            except IndexError:
                field = "version"
            fields = ["type", "vendor", "product", "version"]
            if field not in fields:
                try:
                    field = fields[int(field) - 1]
                except (IndexError, ValueError):
                    field = "version"
            cpeflt = zip(fields, (utils.str2regexp(value) for value in cpeflt))
            # We need two different filters because we need two
            # different $match in the pipeline. The first one occurs
            # before the $unwind operation, so we need an $elemMatch
            # when we filter against more than one value, while the
            # second one occurs after, so an $elemMatch would fail.
            cpeflt1 = self.searchcpe(**dict(
                ("cpe_type" if key == "type" else key, value)
                for key, value in cpeflt
            ))
            cpeflt2 = dict(("cpes.%s" % key, value) for key, value in cpeflt)
            # We need to keep enough cpes.* fields for the projection
            # *and* for our filter
            fields = fields[:max(fields.index(field), len(cpeflt)) + 1]
            flt = self.flt_and(flt, cpeflt1)
            specialproj = dict(("cpes.%s" % fname, 1) for fname in fields)
            specialproj["_id"] = 0
            concat = ["$cpes.%s" % fields[0]]
            # Now we only keep what the user wanted
            for fname in fields[1:fields.index(field) + 1]:
                concat.append(":")
                concat.append("$cpes.%s" % fname)
            specialflt = []
            if cpeflt2:
                specialflt.append({"$match": cpeflt2})
            specialflt.append(
                {"$project": {"cpes.%s" % field: {"$concat": concat}}})
            field = "cpes.%s" % field
            outputproc = lambda x: {'count': x['count'],
                                    '_id': x['_id'].split(':', 3)}
        elif field == 'devicetype':
            field = "ports.service_devicetype"
        elif field.startswith('devicetype:'):
            port = int(field.split(':', 1)[1])
            flt = self.flt_and(flt, self.searchport(port))
            specialproj = {"_id": 0, "ports.port": 1,
                           "ports.service_devicetype": 1}
            specialflt = [
                {"$match": {"ports.port": port}},
                {"$project": {"ports.service_devicetype": 1}}
            ]
            field = "ports.service_devicetype"
        elif field.startswith('smb.'):
            flt = self.flt_and(
                flt, self.searchscript(host=True, name='smb-os-discovery')
            )
            if field == 'smb.dnsdomain':
                field = 'scripts.smb-os-discovery.domain_dns'
            elif field == 'smb.forest':
                field = 'scripts.smb-os-discovery.forest_dns'
            else:
                field = 'scripts.smb-os-discovery.' + field[4:]
        elif field in ["script", "portscript"]:
            flt = self.flt_and(
                flt, self.searchscript(name={"$exists": True})
            )
            field = "ports.scripts.id"
        elif field == "hostscript":
            flt = self.flt_and(
                flt, self.searchscript(name={"$exists": True}, host=True)
            )
            field = "scripts.id"
        elif (field.startswith('script:') or
              field.startswith('portscript:') or
              field.startswith('hostscript:')):
            scriptid = field.split(':', 1)[1]
            flt = self.flt_and(
                flt,
                self.searchscript(name={"$exists": True},
                                  host=field.startswith('hostscript:')),
            )
            if ':' in scriptid:
                base = 'ports.scripts'
                port, scriptid = scriptid.split(':', 1)
                port = int(port)
                flt = self.flt_and(flt, self.searchport(port))
            else:
                base = ("scripts"
                        if field.startswith('hostscript:')
                        else "ports.scripts")
                port, scriptid = None, field.split(':', 1)[1]
            specialproj = {"_id": 0, "%s.id" % base: 1, "%s.output" % base: 1}
            if port is not None:
                specialproj.update({'ports.port': 1})
            specialflt = [
                {"$match": ({"%s.id" % base: scriptid}
                            if port is None else
                            {"ports.scripts.id": scriptid,
                             "ports.port": port})},
                {"$project": {"%s.output" % base: 1}}
            ]
            field = "%s.output" % base
        elif field == 'domains':
            flt = self.flt_and(flt, self.searchdomain({'$exists': True}))
            field = 'hostnames.domains'
        elif field.startswith('domains:'):
            flt = self.flt_and(flt, self.searchdomain({'$exists': True}))
            level = int(field[8:]) - 1
            field = 'hostnames.domains'
            aggrflt = {
                "field": re.compile('^([^\\.]+\\.){%d}[^\\.]+$' % level)}
        elif field.startswith('cert.'):
            subfield = field[5:]
            field = 'ports.scripts.ssl-cert.' + subfield
        elif field == 'sshkey.bits':
            flt = self.flt_and(flt, self.searchsshkey())
            specialproj = {"ports.scripts.ssh-hostkey.type": 1,
                           "ports.scripts.ssh-hostkey.bits": 1}
            specialflt = [{"$project": {
                "_id": 0,
                "ports.scripts.ssh-hostkey.bits": {
                    "$concat": [
                        "$ports.scripts.ssh-hostkey.type",
                        "###",
                        "$ports.scripts.ssh-hostkey.bits",
                    ]}}}]
            field = "ports.scripts.ssh-hostkey.bits"
            outputproc = lambda x: {'count': x['count'],
                                    '_id': x['_id'].split('###')}
        elif field.startswith('sshkey.'):
            flt = self.flt_and(flt, self.searchsshkey())
            subfield = field[7:]
            field = 'ports.scripts.ssh-hostkey.' + subfield
        elif field.startswith('modbus.'):
            subfield = field[7:]
            field = 'ports.scripts.modbus-discover.' + subfield
        elif field.startswith('s7.'):
            subfield = field[3:]
            field = 'ports.scripts.s7-info.' + subfield
        elif field.startswith('enip.'):
            subfield = field[5:]
            subfield = {
                "vendor": "Vendor",
                "product": "Product Name",
                "serial": "Serial Number",
                "devtype": "Device Type",
                "prodcode": "Product Code",
                "rev": "Revision",
                "ip": "Device IP",
            }.get(subfield, subfield)
            field = 'ports.scripts.enip-info.' + subfield
        elif field == 'screenwords':
            field = 'ports.screenwords'
            flt = self.flt_and(flt, self.searchscreenshot(words=True))
        elif field == 'hop':
            field = 'traces.hops.ipaddr'
            outputproc = lambda x: {'count': x['count'],
                                    '_id': utils.int2ip(x['_id'])}
        elif field.startswith('hop') and field[3] in ':>':
            specialproj = {"_id": 0,
                           "traces.hops.ipaddr": 1,
                           "traces.hops.ttl": 1}
            specialflt = [
                {"$match": {
                    "traces.hops.ttl": (
                        int(field[4:])
                        if field[3] == ':' else
                        {"$gt": int(field[4:])}
                    )}},
                {"$project": {"traces.hops.ipaddr": 1}}
            ]
            field = 'traces.hops.ipaddr'
            outputproc = lambda x: {'count': x['count'],
                                    '_id': utils.int2ip(x['_id'])}
        pipeline = self._topvalues(
            field, flt=flt, topnbr=topnbr, sortby=sortby, limit=limit,
            skip=skip, least=least, aggrflt=aggrflt,
            specialproj=specialproj, specialflt=specialflt,
        )
        cursor = self.set_limits(
            self.db[colname].aggregate(pipeline, cursor={})
        )
        if outputproc is not None:
            return (outputproc(res) for res in cursor)
        return cursor

    def parse_args(self, args, flt=None):
        if flt is None:
            flt = self.flt_empty
        if args.category is not None:
            flt = self.flt_and(flt, self.searchcategory(args.category))
        if args.country is not None:
            flt = self.flt_and(flt, self.searchcountry(
                utils.str2list(args.country)))
        if args.asnum is not None:
            flt = self.flt_and(flt, self.searchasnum(
                utils.str2list(args.asnum)))
        if args.asname is not None:
            flt = self.flt_and(flt, self.searchasname(
                utils.str2regexp(args.asname)))
        if args.source is not None:
            flt = self.flt_and(flt, self.searchsource(args.source))
        if args.timeago is not None:
            flt = self.flt_and(flt, self.searchtimeago(args.timeago))
        if args.id is not None:
            flt = self.flt_and(flt, self.searchobjectid(args.id))
        if args.no_id is not None:
            flt = self.flt_and(flt, self.searchobjectid(args.no_id, neg=True))
        if args.host is not None:
            flt = self.flt_and(flt, self.searchhost(args.host))
        if args.hostname is not None:
            flt = self.flt_and(
                flt,
                self.searchhostname(utils.str2regexp(args.hostname))
            )
        if args.domain is not None:
            flt = self.flt_and(
                flt,
                self.searchdomain(utils.str2regexp(args.domain))
            )
        if args.net is not None:
            flt = self.flt_and(flt, self.searchnet(args.net))
        if args.range is not None:
            flt = self.flt_and(flt, self.searchrange(*args.range))
        if args.hop is not None:
            flt = self.flt_and(flt, self.searchhop(args.hop))
        if args.port is not None:
            port = args.port.replace('_', '/')
            if '/' in port:
                proto, port = port.split('/', 1)
            else:
                proto = 'tcp'
            port = int(port)
            flt = self.flt_and(
                flt,
                self.searchport(port=port, protocol=proto))
        if args.not_port is not None:
            not_port = args.not_port.replace('_', '/')
            if '/' in not_port:
                not_proto, not_port = not_port.split('/', 1)
            else:
                not_proto = 'tcp'
            not_port = int(not_port)
            flt = self.flt_and(
                flt,
                self.searchport(port=not_port, protocol=not_proto,
                                neg=True))
        if args.openport:
            flt = self.flt_and(flt, self.searchopenport())
        if args.no_openport:
            flt = self.flt_and(flt, self.searchopenport(neg=True))
        if args.countports:
            minn, maxn = int(args.countports[0]), int(args.countports[1])
            flt = self.flt_and(flt,self.searchcountopenports(minn=minn, maxn=maxn))
        if args.no_countports:
            minn, maxn = int(args.no_countports[0]), int(args.no_countports[1])
            flt = self.flt_and(flt,self.searchcountopenports(minn=minn, maxn=maxn, neg=True))
        if args.service is not None:
            flt = self.flt_and(
                flt,
                self.searchservicescript(utils.str2regexp(args.service)))
        if args.script is not None:
            if ':' in args.script:
                name, output = (utils.str2regexp(string) for
                                string in args.script.split(':', 1))
            else:
                name, output = utils.str2regexp(args.script), None
            flt = self.flt_and(flt, self.searchscript(name=name,
                                                      output=output))
        if args.hostscript is not None:
            if ':' in args.hostscript:
                name, output = (utils.str2regexp(string) for
                                string in args.hostscript.split(':', 1))
            else:
                name, output = utils.str2regexp(args.hostscript), None
            flt = self.flt_and(flt, self.searchscript(host=True,
                                                      name=name,
                                                      output=output))
        if args.svchostname is not None:
            flt = self.flt_and(
                flt,
                self.searchsvchostname(utils.str2regexp(args.svchostname)))
        if args.os is not None:
            flt = self.flt_and(
                flt,
                self.searchos(utils.str2regexp(args.os)))
        if args.anonftp:
            flt = self.flt_and(flt, self.searchftpanon())
        if args.anonldap:
            flt = self.flt_and(flt, self.searchldapanon())
        if args.authhttp:
            flt = self.flt_and(flt, self.searchhttpauth())
        if args.authbypassvnc:
            flt = self.flt_and(flt, self.searchvncauthbypass())
        if args.ypserv:
            flt = self.flt_and(flt, self.searchypserv())
        if args.nfs:
            flt = self.flt_and(flt, self.searchnfs())
        if args.x11:
            flt = self.flt_and(flt, self.searchx11access())
        if args.xp445:
            flt = self.flt_and(flt, self.searchxp445())
        if args.owa:
            flt = self.flt_and(flt, self.searchowa())
        if args.vuln_boa:
            flt = self.flt_and(flt, self.searchvulnintersil())
        if args.torcert:
            flt = self.flt_and(flt, self.searchtorcert())
        if args.sshkey is not None:
            flt = self.flt_and(flt, self.searchsshkey(
                fingerprint=utils.str2regexp(args.sshkey)))
        return flt


class MongoDBPassive(MongoDB, DBPassive):

    def __init__(self, host, dbname,
                 colname_passive="passive",
                 colname_ipdata="ipdata",
                 **kargs):
        MongoDB.__init__(self, host, dbname, **kargs)
        DBPassive.__init__(self)
        self.colname_passive = colname_passive
        self.colname_ipdata = colname_ipdata
        self.indexes = {
            self.colname_passive: [
                [('port', pymongo.ASCENDING)],
                [('value', pymongo.ASCENDING)],
                [('targetval', pymongo.ASCENDING)],
                [('recontype', pymongo.ASCENDING)],
                [('firstseen', pymongo.ASCENDING)],
                [('lastseen', pymongo.ASCENDING)],
                [('sensor', pymongo.ASCENDING)],
                [
                    ('addr', pymongo.ASCENDING),
                    ('recontype', pymongo.ASCENDING),
                    ('port', pymongo.ASCENDING),
                ],
            ],
            self.colname_ipdata: [
                [('country_code', pymongo.ASCENDING)],
                [('location_id', pymongo.ASCENDING)],
                [('as_num', pymongo.ASCENDING)],
            ],
        }
        self.specialindexes = {
            self.colname_passive: [
                # HTTP Auth basic
                ([('infos.username', pymongo.ASCENDING)],
                 {"sparse": True}),
                ([('infos.password', pymongo.ASCENDING)],
                 {"sparse": True}),
                # DNS
                ([('infos.domain', pymongo.ASCENDING)],
                 {"sparse": True}),
                ([('infos.domaintarget', pymongo.ASCENDING)],
                 {"sparse": True}),
                # SSL
                ([('infos.md5hash', pymongo.ASCENDING)],
                 {"sparse": True}),
                ([('infos.sha1hash', pymongo.ASCENDING)],
                 {"sparse": True}),
                ([('infos.issuer', pymongo.ASCENDING)],
                 {"sparse": True}),
                ([('infos.subject', pymongo.ASCENDING)],
                 {"sparse": True}),
                ([('infos.pubkeyalgo', pymongo.ASCENDING)],
                 {"sparse": True}),
            ],
            self.colname_ipdata: [
                ([('addr', pymongo.ASCENDING)],
                 {'unique': True}),
            ],
        }
        try:
            from collections import OrderedDict
        except ImportError:
            self.hint_indexes = {
                "addr": [("addr", 1), ("recontype", 1), ("port", 1)],
                "targetval": [("targetval", 1)],
            }
        else:
            self.hint_indexes = OrderedDict([
                ["addr", [("addr", 1), ("recontype", 1), ("port", 1)]],
                ["targetval", [("targetval", 1)]],
            ])

    def init(self):
        """Initializes the "passive" columns, i.e., drops the columns, and
creates the default indexes."""
        self.db[self.colname_passive].drop()
        self.db[self.colname_ipdata].drop()
        self.create_indexes()

    def get(self, spec, **kargs):
        """Queries the passive column with the provided filter "spec", and
returns a MongoDB cursor.

This should be very fast, as no operation is done (the cursor is only
returned). Next operations (e.g., .count(), enumeration, etc.) might
take a long time, depending on both the operations and the filter.

Any keyword argument is passed to the .find() method of the Mongodb
column object, without any validation (and might have no effect if it
is not expected)."""
        return self.set_limits(
            self.find(self.colname_passive, spec, **kargs))

    def get_one(self, spec, **kargs):
        """Same function as get, except .find_one() method is called
instead of .find(), so the first record matching "spec" (or None) is
returned.

Unlike get(), this function might take a long time, depending
on "spec" and the indexes set on colname_passive column."""
        # TODO: check limits
        return self.find_one(self.colname_passive, spec, **kargs)

    def update(self, spec, **kargs):
        """Updates the first record matching "spec" in the "passive" column,
setting values according to the keyword arguments.
"""
        self.db[self.colname_passive].update(spec, {'$set': kargs})

    def insert(self, spec, getinfos=None):
        """Inserts the record "spec" into the passive column."""
        if getinfos is not None:
            spec.update(getinfos(spec))
        self.db[self.colname_passive].insert(spec)
        if 'addr' in spec:
            self.set_data(spec['addr'])

    def insert_or_update(self, timestamp, spec, getinfos=None):
        if spec is None:
            return
        hint = self.get_hint(spec)
        current = self.get(spec, fields=[])
        if hint is not None:
            current.hint(hint)
        try:
            current = current[0]
        except IndexError:
            current = None
        updatespec = {
            '$inc': {'count': 1},
            '$min': {'firstseen': timestamp},
            '$max': {'lastseen': timestamp},
        }
        if current is not None:
            self.db[self.colname_passive].update(
                {'_id': current['_id']},
                updatespec,
            )
        else:
            if getinfos is not None:
                infos = getinfos(spec)
                if infos:
                    updatespec['$setOnInsert'] = infos
            self.db[self.colname_passive].update(
                spec,
                updatespec,
                upsert=True,
            )

    def insert_or_update_bulk(self, specs, getinfos=None):
        """Like `.insert_or_update()`, but `specs` parameter has to be
        an iterable of (timestamp, spec) values. This will perform
        bulk MongoDB inserts with the major drawback that the
        `getinfos` parameter will be called (if it is not `None`) for
        each spec, the spec already exists in the database and the
        call was hence unnecessary.

        It's up to you to decide whether having bulk insert is worth
        it or if you want to go with the regular `.insert_or_update()`
        method.

        """
        bulk = self.db[self.colname_passive].initialize_unordered_bulk_op()
        count = 0
        try:
            for timestamp, spec in specs:
                if spec is not None:
                    updatespec = {
                        '$inc': {'count': 1},
                        '$min': {'firstseen': timestamp},
                        '$max': {'lastseen': timestamp},
                    }
                    if getinfos is not None:
                        infos = getinfos(spec)
                        if infos:
                            updatespec['$setOnInsert'] = infos
                    bulk.find(spec).upsert().update(updatespec)
                    count += 1
                    if count >= config.BULK_UPSERTS_MAXSIZE:
                        if config.DEBUG:
                            print "MongoDB bulk upsert: %d" % count
                        bulk.execute()
                        bulk = self.db[self.colname_passive]\
                                   .initialize_unordered_bulk_op()
                        count = 0
        except IOError:
            pass
        if count > 0:
            if config.DEBUG:
                print "MongoDB bulk upsert: %d (final)" % count
            bulk.execute()

    def insert_or_update_mix(self, spec, getinfos=None):
        """Updates the first record matching "spec" (without
        "firstseen", "lastseen" and "count") by mixing "firstseen",
        "lastseen" and "count" from "spec" and from the database.

        This is usefull to mix records from different databases.

        """
        updatespec = {}
        if 'firstseen' in spec:
            updatespec['$min'] = {'firstseen': spec['firstseen']}
            del spec['firstseen']
        if 'lastseen' in spec:
            updatespec['$max'] = {'lastseen': spec['lastseen']}
            del spec['lastseen']
        if 'count' in spec:
            updatespec['$inc'] = {'count': spec['count']}
            del spec['count']
        else:
            updatespec['$inc'] = {'count': spec['count']}
        if 'infos' in spec:
            updatespec['$setOnInsert'] = {'infos': spec['infos']}
            del spec['infos']
        if 'fullinfos' in spec:
            if '$setOnInsert' in updatespec:
                updatespec['$setOnInsert'].update(
                    {'fullinfos': spec['fullinfos']}
                )
            else:
                updatespec['$setOnInsert'] = {'fullinfos': spec['fullinfos']}
            del spec['fullinfos']
        current = self.get_one(spec, fields=[])
        if current:
            self.db[self.colname_passive].update(
                {'_id': current['_id']},
                updatespec,
            )
        else:
            if getinfos is not None and "$setOnInsert" not in updatespec:
                infos = getinfos(spec)
                if infos:
                    updatespec['$setOnInsert'] = infos
            self.db[self.colname_passive].update(
                spec,
                updatespec,
                upsert=True,
            )

    def remove(self, spec):
        self.db[self.colname_passive].remove(spec)

    def topvalues(self, field, distinct=True, **kargs):
        """This method makes use of the aggregation framework to
        produce top values for a given field.

        If `distinct` is True (default), the top values are computed
        by distinct events. If it is False, they are computed based on
        the "count" field.

        """
        if not distinct:
            kargs['countfield'] = 'count'
        pipeline = self._topvalues(field, **kargs)
        return self.set_limits(
            self.db[self.colname_passive].aggregate(pipeline, cursor={})
        )

    @staticmethod
    def searchsensor(sensor, neg=False):
        if neg:
            if type(sensor) is utils.REGEXP_T:
                return {'sensor': {'$not': sensor}}
            return {'sensor': {'$ne': sensor}}
        return {'sensor': sensor}

    @staticmethod
    def searchuseragent(useragent):
        return {
            'recontype': 'HTTP_CLIENT_HEADER',
            'source': 'USER-AGENT',
            'value': useragent
        }

    @staticmethod
    def searchdns(name, reverse=False, subdomains=False):
        return {
            'recontype': 'DNS_ANSWER',
            (('infos.domaintarget' if reverse else 'infos.domain')
             if subdomains else ('targetval' if reverse else 'value')): name,
        }

    @staticmethod
    def searchcert():
        return {'recontype': 'SSL_SERVER',
                'source': 'cert'}

    @staticmethod
    def searchcertsubject(expr):
        return {'recontype': 'SSL_SERVER',
                'source': 'cert',
                'infos.subject': expr}

    @staticmethod
    def searchcertissuer(expr):
        return {'recontype': 'SSL_SERVER',
                'source': 'cert',
                'infos.issuer': expr}

    @staticmethod
    def searchbasicauth():
        return {
            'recontype': {'$in': ['HTTP_CLIENT_HEADER',
                                  'HTTP_CLIENT_HEADER_SERVER']},
            'source': {'$in': ['AUTHORIZATION',
                               'PROXY-AUTHORIZATION']},
            'value': re.compile('^Basic'),
        }

    @staticmethod
    def searchhttpauth():
        return {
            'recontype': {'$in': ['HTTP_CLIENT_HEADER',
                                  'HTTP_CLIENT_HEADER_SERVER']},
            'source': {'$in': ['AUTHORIZATION',
                               'PROXY-AUTHORIZATION']},
        }

    @staticmethod
    def searchftpauth():
        return {'recontype': {'$in': ['FTP_CLIENT', 'FTP_SERVER']}}

    @staticmethod
    def searchpopauth():
        return {'recontype': {'$in': ['POP_CLIENT', 'POP_SERVER']}}

    def searchcountry(self, code, neg=False):
        return {'addr': {'$nin' if neg else '$in':
                         self.knownip_bycountry(code)}}

    def searchasnum(self, asnum, neg=False):
        return {'addr': {'$nin' if neg else '$in': self.knownip_byas(asnum)}}

    @staticmethod
    def searchtimeago(delta, neg=False, new=False):
        field = 'lastseen' if new else 'firstseen'
        if isinstance(delta, datetime.timedelta):
            delta = delta.total_seconds()
        now = datetime.datetime.now()
        now = int(now.strftime('%s')) + now.microsecond * 1e-6
        return {field: {'$lt' if neg else '$gte': now - delta}}

    def knownip_bycountry(self, code):
        return self.set_limits(self.find(self.colname_ipdata,
                                         {'country_code': code}
                                     )).distinct('addr')

    def knownip_byas(self, asnum):
        if type(asnum) is str:
            if asnum.startswith('AS'):
                asnum = asnum[2:]
            asnum = int(asnum)
        return self.set_limits(self.find(self.colname_ipdata,
                                         {'as_num': asnum}
                                     )).distinct('addr')

    def set_data(self, addr, force=False):
        """Sets IP information in colname_ipdata."""
        if not force and self.get_data(addr) is not None:
            return
        for data in [self.globaldb.data.country_byip(addr),
                     self.globaldb.data.as_byip(addr),
                     self.globaldb.data.location_byip(addr)]:
            if data is not None:
                self.db[self.colname_ipdata].update(
                    {'addr': addr},
                    {'$set': data},
                    upsert=True)

    def update_country(self, start, stop, code, create=False):
        # we first update existing records
        self.db[self.colname_ipdata].update(
            self.searchrange(start, stop),
            {'$set': {'country_code': code}})
        # then (if requested), we add a record for addresses we
        # have in database (first the active one, then the
        # passive)
        if create:
            for addr in self.get_known_ips(start, stop):
                self.db[self.colname_ipdata].update(
                    {'addr': addr},
                    {'$set': {'country_code': code}},
                    upsert=True)

    def update_city(self, start, stop, locid, create=False):
        # we first update existing records
        self.db[self.colname_ipdata].update(
            self.searchrange(start, stop),
            {'$set': {'location_id': locid}})
        # then (if requested), we add a record for addresses we
        # have in database (first the active one, then the
        # passive)
        if create:
            for addr in self.get_known_ips(start, stop):
                self.db[self.colname_ipdata].update(
                    {'addr': addr},
                    {'$set': {'location_id': locid}},
                    upsert=True)

    def update_as(self, start, stop, asnum, asname, create=False):
        if asname is None:
            updatespec = {'as_num': asnum}
        else:
            updatespec = {'as_num': asnum, 'as_name': asname}
        # we first update existing records
        self.db[self.colname_ipdata].update(
            self.searchrange(start, stop),
            {'$set': updatespec})
        # then (if requested), we add a record for addresses we
        # have in database (first the active one, then the
        # passive)
        if create:
            for addr in self.get_known_ips(start, stop):
                self.db[self.colname_ipdata].update(
                    {'addr': addr},
                    {'$set': updatespec},
                    upsert=True)

    def get_data(self, addr):
        """Gets IP information in colname_ipdata."""
        data = self.find_one(self.colname_ipdata, {'addr': addr})
        if data is not None:
            del data['_id']
        return data

    def get_known_ips(self, start, stop):
        self.get(self.searchrange(start, stop)).distinct('addr')


class MongoDBData(MongoDB, DBData):

    def __init__(self, host, dbname,
                 colname_geoip_country="geoipcountry",
                 colname_geoip_as="geoipas",
                 colname_geoip_city="geoipcity",
                 colname_country_codes="countries",
                 colname_city_locations="cities",
                 **kargs):
        MongoDB.__init__(self, host, dbname, **kargs)
        DBData.__init__(self)
        self.colname_geoip_country = colname_geoip_country
        self.colname_geoip_as = colname_geoip_as
        self.colname_geoip_city = colname_geoip_city
        self.colname_country_codes = colname_country_codes
        self.colname_city_locations = colname_city_locations
        self.indexes = {
            self.colname_geoip_country: [
                [('start', pymongo.ASCENDING)],
                [('country_code', pymongo.ASCENDING)],
            ],
            self.colname_geoip_as: [
                [('start', pymongo.ASCENDING)],
                [('as_num', pymongo.ASCENDING)],
            ],
            self.colname_geoip_city: [
                [('start', pymongo.ASCENDING)],
                [('location_id', pymongo.ASCENDING)],
            ],
            self.colname_city_locations: [
                [('location_id', pymongo.ASCENDING)],
                [('country_code', pymongo.ASCENDING)],
                [('region_code', pymongo.ASCENDING)],
                [('city', pymongo.ASCENDING)],
                [('loc', pymongo.GEOSPHERE)],
            ],
        }
        self.specialindexes = {
            self.colname_country_codes: [
                ([('country_code', pymongo.ASCENDING)],
                 {'unique': True}),
            ],
        }

    def init(self):
        """Initializes the data columns, and creates the default
        indexes.

        """
        self.db[self.colname_geoip_country].drop()
        self.db[self.colname_geoip_as].drop()
        self.db[self.colname_geoip_city].drop()
        self.db[self.colname_country_codes].drop()
        self.db[self.colname_city_locations].drop()
        self.create_indexes()

    def feed_geoip_country(self, fname, feedipdata=None,
                           createipdata=False):
        self.country_codes = {}
        with open(fname) as fdesc:
            self.db[self.colname_geoip_country].insert(
                self.parse_line_country(line, feedipdata=feedipdata,
                                        createipdata=createipdata)
                for line in fdesc
            )
        self.db[self.colname_country_codes].insert(
            {'country_code': code, 'name': name}
            for code, name in self.country_codes.iteritems()
        )
        self.country_codes = None

    def feed_geoip_city(self, fname, feedipdata=None,
                        createipdata=False):
        with open(fname) as fdesc:
            # Skip the two first lines
            fdesc.readline()
            fdesc.readline()
            self.db[self.colname_geoip_city].insert(
                self.parse_line_city(line, feedipdata=feedipdata,
                                     createipdata=createipdata)
                for line in fdesc
            )

    def feed_city_location(self, fname):
        with open(fname) as fdesc:
            # Skip the two first lines
            fdesc.readline()
            fdesc.readline()
            self.db[self.colname_city_locations].insert(
                self.parse_line_city_location(line)
                for line in fdesc)

    def feed_geoip_asnum(self, fname, feedipdata=None,
                         createipdata=False):
        with open(fname) as fdesc:
            self.db[self.colname_geoip_as].insert(
                self.parse_line_asnum(line, feedipdata=feedipdata,
                                      createipdata=createipdata)
                for line in fdesc
            )

    def country_name_by_code(self, code):
        rec = self.find_one(self.colname_country_codes,
                            {'country_code': code},
                            fields=['name'])
        if rec:
            return rec['name']
        return rec

    def country_codes_by_name(self, name):
        return self.set_limits(
            self.find(self.colname_country_codes,
                      {'name': name})).distinct('country_code')

    def find_data_byip(self, addr, column):
        try:
            addr = utils.ip2int(addr)
        except (TypeError, utils.socket.error):
            pass
        rec = self.find_one(column, {'start': {'$lte': addr}},
                            sort=[('start', -1)])
        if rec and addr <= rec['stop']:
            del rec['_id'], rec['start'], rec['stop']
            return rec

    def country_byip(self, addr):
        rec = self.find_data_byip(addr, self.colname_geoip_country)
        if rec:
            name = self.country_name_by_code(rec['country_code'])
            if name:
                rec['country_name'] = name
        return rec

    def as_byip(self, addr):
        return self.find_data_byip(addr, self.colname_geoip_as)

    def locationid_byip(self, addr):
        return self.find_data_byip(addr, self.colname_geoip_city)

    def location_byid(self, locid):
        rec = self.find_one(self.colname_city_locations,
                            {'location_id': locid})
        if rec:
            del rec['_id'], rec['location_id']
        return rec

    def location_byip(self, addr):
        locid = self.locationid_byip(addr)
        if locid:
            return self.location_byid(locid.get('location_id'))

    def infos_byip(self, addr):
        infos = {}
        for infos_byip in [self.country_byip,
                           self.as_byip,
                           self.location_byip]:
            newinfos = infos_byip(addr)
            if newinfos is not None:
                infos.update(newinfos)
        if infos:
            return infos

    def ipranges_bycountry(self, code):
        return [
            (x['start'], x['stop']) for x in
            self.set_limits(
                self.find(self.colname_geoip_country,
                          {'country_code': code},
                          fields=['start', 'stop']))
        ]

    def ipranges_byas(self, asnum):
        if type(asnum) is str:
            if asnum.startswith('AS'):
                asnum = asnum[2:]
            asnum = int(asnum)
        return [
            (x['start'], x['stop']) for x in
            self.set_limits(
                self.find(self.colname_geoip_as,
                          {'as_num': asnum},
                          fields=['start', 'stop']))
        ]


class MongoDBAgent(MongoDB, DBAgent):
    """MongoDB-specific code to handle agents-in-DB"""

    def __init__(self, host, dbname,
                 colname_agents="agents",
                 colname_scans="runningscans",
                 colname_masters="masters",
                 **kargs):
        MongoDB.__init__(self, host, dbname, **kargs)
        DBAgent.__init__(self)
        self.colname_agents = colname_agents
        self.colname_scans = colname_scans
        self.colname_masters = colname_masters
        self.indexes = {
            self.colname_agents: [
                [('host', pymongo.ASCENDING)],
                [('path.remote', pymongo.ASCENDING)],
                [('path.local', pymongo.ASCENDING)],
                [('master', pymongo.ASCENDING)],
                [('scan', pymongo.ASCENDING)],
            ],
            self.colname_scans: [
                [('agents', pymongo.ASCENDING)],
            ],
            self.colname_masters: [
                [('hostname', pymongo.ASCENDING),
                 ('path', pymongo.ASCENDING)],
            ],
        }

    def init(self):
        """Initializes the "agent" columns, i.e., drops those columns
        and creates the default indexes.

        """
        self.db[self.colname_agents].drop()
        self.db[self.colname_scans].drop()
        self.db[self.colname_masters].drop()
        self.create_indexes()

    def stop_agent(self, agentid):
        agent = self.get_agent(agentid)
        if agent is None:
            raise IndexError("Agent not found [%r]" % agentid)
        if agent['scan'] is not None:
            self.unassign_agent(agent['_id'])

    def _add_agent(self, agent):
        return self.db[self.colname_agents].insert(agent)

    def get_agent(self, agentid):
        return self.find(self.colname_agents, {"_id": agentid})

    def get_free_agents(self):
        return (x['_id'] for x in
                self.set_limits(
                    self.find(self.colname_agents,
                              {"scan": None},
                              fields=["_id"])))

    def get_agents_by_master(self, masterid):
        return (x['_id'] for x in
                self.set_limits(
                    self.find(self.colname_agents,
                              {"master": masterid},
                              fields=["_id"])))

    def get_agents(self):
        return (x['_id'] for x in
                self.set_limits(
                    self.find(self.colname_agents,
                              fields=["_id"])))

    def assign_agent(self, agentid, scanid,
                     only_if_unassigned=False,
                     force=False):
        flt = {"_id": agentid}
        if only_if_unassigned:
            flt.update({"scan": None})
        elif not force:
            flt.update({"scan": {"$ne": False}})
        self.db[self.colname_agents].update(
            flt,
            {"$set": {"scan": scanid}}
        )
        agent = self.get_agent(agentid)
        if scanid is not None and scanid is not False \
           and scanid == agent["scan"]:
            self.db[self.colname_scans].update(
                {"_id": scanid, "agents": {"$ne": agentid}},
                {"$push": {"agents": agentid}}
            )

    def unassign_agent(self, agentid, dont_reuse=False):
        agent = self.get_agent(agentid)
        scanid = agent["scan"]
        if scanid is not None:
            self.db[self.colname_scans].update(
                {"_id": scanid, "agents": agentid},
                {"$pull": {"agents": agentid}}
            )
        if dont_reuse:
            self.assign_agent(agentid, False, force=True)
        else:
            self.assign_agent(agentid, None, force=True)

    def _del_agent(self, agentid):
        return self.db[self.colname_agents].remove(spec_or_id=agentid)

    def _add_scan(self, scan):
        return self.db[self.colname_scans].insert(scan)

    def _get_scan(self, scanid):
        return self.find_one(self.colname_scans, {"_id": scanid})

    def _lock_scan(self, scanid, oldlockid, newlockid):
        if oldlockid is not None:
            oldlockid = bson.Binary(oldlockid)
        if newlockid is not None:
            newlockid = bson.Binary(newlockid)
        result = self.db[self.colname_scans].find_and_modify({
            "_id": scanid,
            "lock": oldlockid,
        }, {
            "$set": {"lock": newlockid}
        }, full_response=True, new=True)['value']
        if result is not None and result['lock'] is not None:
            result['lock'] = str(result['lock'])
        return result

    def _unlock_scan(self, scanid, lockid):
        result = self.db[self.colname_scans].find_and_modify({
            "_id": scanid,
            "lock": bson.Binary(lockid),
        }, {
            "$set": {"lock": None}
        }, full_response=True, new=True)['value']
        if result is not None and result['lock'] is not None:
            result['lock'] = str(result['lock'])
        return result

    def get_scans(self):
        return (x['_id'] for x in
                self.set_limits(
                    self.find(self.colname_scans,
                              fields=["_id"])))

    def _update_scan_target(self, scanid, target):
        return self.db[self.colname_scans].update(
            {"_id": scanid}, {"$set": {"target": target}})

    def incr_scan_results(self, scanid):
        return self.db[self.colname_scans].update(
            {"_id": scanid}, {"$inc": {"results": 1}})

    def _add_master(self, master):
        return self.db[self.colname_masters].insert(master)

    def get_master(self, masterid):
        return self.find_one(self.colname_masters, {"_id": masterid})

    def get_masters(self):
        return (x['_id'] for x in
                self.set_limits(
                    self.find(self.colname_masters,
                              fields=["_id"])))
