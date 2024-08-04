import copy
import pickle
import os
import re
import asyncio
import time
from json.decoder import JSONDecodeError
from threading import Thread
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import HTTPError
from datetime import datetime
from io import BytesIO

from duckduckgo_search import DDGS
from itertools import islice

from lxml import etree

import json

import logging
logging.basicConfig(format='%(asctime)s [%(levelname)s]: %(message)s',filename='scrobbler.log',level=logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.WARN)
logging.getLogger("httpcore").setLevel(logging.WARN)
logging.getLogger("hpack").setLevel(logging.INFO)
logging.getLogger("requests").setLevel(logging.INFO)
logging.getLogger("pyatv").setLevel(logging.WARN)
stderrLogger=logging.StreamHandler()
stderrLogger.setFormatter(logging.Formatter('[%(levelname)s]: %(message)s'))
logging.getLogger().addHandler(stderrLogger)


from trakt import Trakt
from media_remote import MediaRemoteProtocol
from pyatv.protocols.mrp.protobuf import ProtocolMessage, Common_pb2
from pyatv.protocols.mrp.messages import create

cocoa_time = datetime(2001, 1, 1)


class ScrobblingRemoteProtocol(MediaRemoteProtocol):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.now_playing_metadata = None
        self.now_playing_description = None
        self.current_player = None
        self.playback_state = None
        self.pending_playback_state = None
        self.last_elapsed_time = None
        self.last_elapsed_time_timestamp = None
        self.last_trakt_request_timestamp = 0
        self.netflix_titles = {}
        self.itunes_titles = {}
        self.amazon_titles = {}
        self.app_handlers = {'com.apple.TVShows': self.handle_tvshows,
                             'com.apple.TVWatchList': self.handle_tv_app,
                             'com.apple.TVMovies': self.handle_movies,
                             'com.netflix.Netflix': self.handle_netflix,
                             'com.amazon.aiv.AIVApp': self.handle_amazon}

        Trakt.configuration.defaults.client(id='dc705f550f50706bdd7bd55db120235cc68899dbbfb4fbc171384c1c1d30d7d4',
                                            secret='f9aba211b886ea9f31a57c952cd0b5ab702501808db50584a24a5cc07466179d')
        Trakt.on('oauth.token_refreshed', self.on_trakt_token_refreshed)
        self.authenticate_trakt()

    def authenticate_trakt(self):
        if os.path.exists('data/trakt.auth'):
            response = pickle.load(open('data/trakt.auth', 'rb'))
        else:
            logging.info('Navigate to %s' % Trakt['oauth'].authorize_url('urn:ietf:wg:oauth:2.0:oob'))
            pin = input('Authorization code: ')
            response = Trakt['oauth'].token(pin, 'urn:ietf:wg:oauth:2.0:oob')
            self.on_trakt_token_refreshed(response)
        Trakt.configuration.defaults.oauth.from_response(response, refresh=True)

    async def connect(self, atv):
        await super().connect(atv)
        protocol = self.atv.remote_control.main_instance.protocol
        protocol.listen_to(ProtocolMessage.SET_STATE_MESSAGE, self.message_received)
        protocol.listen_to(ProtocolMessage.REMOVE_PLAYER_MESSAGE, self.message_received)
        protocol.listen_to(ProtocolMessage.UPDATE_CONTENT_ITEM_MESSAGE, self.message_received)
        protocol.listen_to(ProtocolMessage.TRANSACTION_MESSAGE, self.message_received)
        protocol.listen_to(ProtocolMessage.DEVICE_INFO_MESSAGE, self.message_received)

    async def message_received(self, msg):
        logging.info("------------------------------")
        if msg.type == ProtocolMessage.SET_STATE_MESSAGE:
            logging.debug("Set state")
            state_msg = msg.inner()
            if state_msg.HasField('playerPath'):
                logging.debug("playerpath")
                self.current_player = state_msg.playerPath.client.bundleIdentifier

            if len(state_msg.playbackQueue.contentItems) > 0:
                logging.debug("metadata")
                content_item = state_msg.playbackQueue.contentItems[0]
                if content_item.HasField('metadata') and content_item.metadata.ByteSize() > 0:
                    self.set_metadata(content_item.metadata)
            elif state_msg.HasField('playbackQueue'):
                # Amazon Prime doesn't remove player, only pauses the playback
                self.stop_scrobbling()
                
            if state_msg.HasField('playbackState'):
                logging.debug("playbackState: " + str(state_msg.playbackState))
                prevPlaybackState = self.playback_state
                if self.is_invalid_metadata():
                    self.pending_playback_state = state_msg.playbackState
                else:
                    self.playback_state = state_msg.playbackState
                self.update_scrobbling(prevPlaybackState=prevPlaybackState)
        elif msg.type == ProtocolMessage.REMOVE_PLAYER_MESSAGE:
            logging.debug("Remove player")
            self.stop_scrobbling()
        elif msg.type == ProtocolMessage.UPDATE_CONTENT_ITEM_MESSAGE:
            logging.debug("Update content")
            updateMsg = msg.inner()
            content_item = updateMsg.contentItems[0]
            if content_item.HasField("metadata") and content_item.metadata.ByteSize() > 0:
                self.set_metadata(content_item.metadata)

    def post_trakt_update(self, operation, done=None):
        if self.is_invalid_metadata():
            return

        def inner():
            cur_timestamp = time.time()
            wait = self.last_trakt_request_timestamp + 1 - cur_timestamp
            self.last_trakt_request_timestamp = cur_timestamp
            if wait > 0:
                self.last_trakt_request_timestamp += wait
                time.sleep(wait)

            if self.current_player in self.app_handlers:
                handler = self.app_handlers[self.current_player]
                if handler is not None:
                    try:
                        # noinspection PyArgumentList
                        handler(operation, self.progress())
                    except ConnectionError:
                        pass
                if done is not None:
                    done()
        Thread(target=lambda: inner()).start()

    def progress(self):
        elapsed_time = self.now_playing_metadata.elapsedTime
        cur_cocoa_time = (datetime.utcnow() - cocoa_time).total_seconds()
        increment = cur_cocoa_time - self.now_playing_metadata.elapsedTimeTimestamp
        if increment > 5 and elapsed_time + increment < self.now_playing_metadata.duration:
            elapsed_time += increment
        progress = elapsed_time * 100 / self.now_playing_metadata.duration
        return progress

    def set_metadata(self, metadata):
        if self.is_invalid_metadata():
            self.playback_state = self.pending_playback_state
            prevPlaybackState = Common_pb2.PlaybackState.Stopped
        else:
            prevPlaybackState = None

        newMetadata = copy.copy(metadata)

        if self.now_playing_metadata is not None and self.now_playing_metadata.title != newMetadata.title:
            self.last_elapsed_time = None
            self.last_elapsed_time_timestamp = None
            self.now_playing_description = None

        self.now_playing_metadata = newMetadata
        self.update_scrobbling(prevPlaybackState=prevPlaybackState)

    def is_invalid_metadata(self):
        return self.now_playing_metadata is None or self.now_playing_metadata.duration < 300 or len(
            self.now_playing_metadata.title) == 0

    def update_scrobbling(self, prevPlaybackState=None):
        if self.is_invalid_metadata():
            return
        if self.current_player not in self.app_handlers:
            return

        if self.playback_state == Common_pb2.PlaybackState.Playing:
            if self.last_elapsed_time is not None:
                timestampDiff = self.now_playing_metadata.elapsedTimeTimestamp - self.last_elapsed_time_timestamp
                elapsedDiff = self.now_playing_metadata.elapsedTime - self.last_elapsed_time
                if abs(timestampDiff - elapsedDiff) > 5:
                    self.post_trakt_update(Trakt['scrobble'].start)
            self.last_elapsed_time = self.now_playing_metadata.elapsedTime
            self.last_elapsed_time_timestamp = self.now_playing_metadata.elapsedTimeTimestamp

        if prevPlaybackState != self.playback_state and prevPlaybackState is not None:
            if self.playback_state == Common_pb2.PlaybackState.Paused:
                progress = self.progress()
                logging.info("Stop with progress " + str(progress))
                if abs(progress) > 92:
                    self.stop_scrobbling()
                else:
                    self.post_trakt_update(Trakt['scrobble'].pause)
            elif self.playback_state == Common_pb2.PlaybackState.Playing:
                self.post_trakt_update(Trakt['scrobble'].start)

    def stop_scrobbling(self):
        self.playback_state = None
        self.pending_playback_state = None
        self.last_elapsed_time = None
        self.last_elapsed_time_timestamp = None

        def cleanup():
            self.now_playing_metadata = None
            self.now_playing_description = None
            self.current_player = None

        if not self.is_invalid_metadata():
            self.post_trakt_update(Trakt['scrobble'].stop, cleanup)
        else:
            cleanup()

    def handle_tv_app(self, operation, progress):
        self.handle_tvshows(operation, progress)

    def handle_tvshows(self, operation, progress):
        if self.now_playing_metadata.HasField('seasonNumber'):
            logging.info("ATV: Season detected")
            season_number = self.now_playing_metadata.seasonNumber
            episode_number = self.now_playing_metadata.episodeNumber
        else:
            logging.info("ATV: searching content id")
            info = self.get_itunes_title(self.now_playing_metadata.contentIdentifier)
            if info is None:
                return
            season_number, episode_number = info
        operation(show={'title': self.get_title()},
                  episode={'season': season_number, 'number': episode_number},
                  progress=progress)

    def get_title(self):
        if self.now_playing_metadata is not None:
            title = self.now_playing_metadata.seriesName
            if len(title) == 0:
                title = self.now_playing_metadata.title
            logging.info("ATV found: " + title)
            return title
        return None

    def handle_movies(self, operation, progress):
        movie = {}
        match = re.search('(.*) \\((\\d\\d\\d\\d)\\)', self.now_playing_metadata.title)
        if match is None:
            movie['title'] = self.now_playing_metadata.title
        else:
            movie['title'] = match.group(1)
            movie['year'] = match.group(2)
        operation(movie=movie, progress=progress)

    def get_itunes_title(self, contentIdentifier):
        known = self.itunes_titles.get(contentIdentifier)
        if known:
            return known['season'], known['episode']

        try:
            logging.debug("ATV: "+ 'https://itunes.apple.com/lookup?country=de&id=' + contentIdentifier)
            result = json.loads(urlopen('https://itunes.apple.com/lookup?country=de&id=' + contentIdentifier).read()
                            .decode('utf-8'))
            result = result['results'][0]
            logging.debug("ATV: title: " + result['trackName'])
            match = re.match("^(Season|Series) (\\d\\d?), Episode (\\d\\d?): ", result['trackName'])
            if match is not None:
                season = int(match.group(2))
                episode = int(match.group(3))
            else:
                logging.debug("ATV: title2: " + result['collectionName'])
                season = int(re.match(".*, Season ([0-9]+)( \\(Uncensored\\))?$", result['collectionName']).group(1))
                episode = int(result['trackNumber'])
        except HTTPError:
            result = self.get_apple_tv_plus_info(self.get_title())
            if not result:
                return None
            season, episode = result
            
        self.itunes_titles[contentIdentifier] = {'season': season, 'episode': episode}
        return season, episode

    def handle_netflix(self, operation, progress):
        logging.info("NF: " + self.now_playing_metadata.title)
        match = re.match('^S(\\d\\d?): E(\\d\\d?) (.*)', self.now_playing_metadata.title)
        if match is not None:
            key = self.now_playing_metadata.title + str(self.now_playing_metadata.duration)
            title = self.netflix_titles.get(key)

            if not title:
                if self.now_playing_metadata.contentIdentifier:
                    logging.info("NF: contentId detected" )
                    title = self.get_netflix_title(self.now_playing_metadata.contentIdentifier)
                else:
                    logging.info("NF: Searching with description")
                    title = self.get_netflix_title_from_description(match.group(3))
                    if not title:
                        return
                self.netflix_titles[key] = title
            if title:
                logging.info("NF: Found title: " + title)
                operation(show={'title': title},
                          episode={'season': match.group(1), 'number': match.group(2)},
                          progress=progress)
        else:
            logging.info("NF: Match movie")
            operation(movie={'title': self.now_playing_metadata.title}, progress=progress)

    def search_by_description(self, query):
        if not self.now_playing_description:
            self.request_now_playing_description()

        query += ' "' + self.now_playing_description + '"'
        logging.debug("DDG: searching: " + query)
        try:
            with DDGS() as ddgs:
                ddgs_gen = ddgs.text(query)
                results = [x for x in islice(ddgs_gen, 1)]
                logging.debug(results)
            return str(results)
        except AssertionError:
            logging.debug("error")
            return None

    def get_netflix_title_from_description(self, episode_title):
        data = self.search_by_description("site:netflix.com")


        if not data:
            return None

        match = re.search('netflix\\.com/(.+?/)?title/(\\d+)', data)
        if not match:
            logging.info("NF: Nothing matched")
            return self.get_trakt_title_from_description(episode_title)
        contentIdentifier = match.group(2)
        title = self.get_netflix_title(contentIdentifier)
        return title.replace(" (U.S.)", "")

    def get_trakt_title_from_description(self, episode_title):
        data = self.search_by_description("site:trakt.tv")

        if not data:
            return None

        match = re.search('trakt\\.tv/shows/(.+?)/', data)
        if not match:
            logging.info("TR: Nothing matched")
            return None
        title = match.group(1)
        return title.replace("-", " ")

    def get_apple_tv_plus_info(self, title):
        data = self.search_by_description("site:tv.apple.com " + title)

        logging.debug(data)
        if not data:
            return None


        match = re.search("Season ([0-9]+), Episode ([0-9]+)", data)
        if match is not None:
            season = int(match.group(1))
            episode = int(match.group(2))
            return season, episode

        match = re.search("S([0-9]+) E([0-9]+)", data)
        if match is not None:
            season = int(match.group(1))
            episode = int(match.group(2))
            return season, episode


        match = re.search('(https://tv\\.apple\\.com/(../)?episode/.*?)\"', data)
        if not match:
            return None

        try:
            data = urlopen(match.group(1)).read()
        except HTTPError:
            return None

        xml = etree.parse(BytesIO(data), etree.HTMLParser())
        for script in xml.xpath('//script'):
            if not script.text:
                continue
            try:
                for d in list(json.loads(script.text).values()):
                    if type(d) is not str:
                        continue
                    try:
                        d = json.loads(d)
                        if 'd' in d and 'data' in d['d'] and 'content' in d['d']['data']:
                            info = d['d']['data']['content']
                            if 'seasonNumber' in info:
                                return info['seasonNumber'], info['episodeNumber']
                    except JSONDecodeError:
                        continue
            except JSONDecodeError:
                continue

        return None

    @staticmethod
    def get_netflix_title(contentIdentifier):
        logging.info("NF: fetch content id")
        data = urlopen('https://www.netflix.com/title/' + contentIdentifier).read()
        xml = etree.parse(BytesIO(data), etree.HTMLParser())
        info = json.loads(xml.xpath('//script')[0].text)
        return info['name']

    def handle_amazon(self, operation, progress):
        title, season, episode = self.get_amazon_details(self.now_playing_metadata.contentIdentifier)
        operation(show={'title': title},
                  episode={'season': season, 'number': episode},
                  progress=progress)

    def get_amazon_details(self, contentIdentifier):
        contentIdentifier = contentIdentifier.replace(":DE", "")
        known = self.amazon_titles.get(contentIdentifier)
        if known:
            return known['title'], known['season'], known['episode']
        url = self.config['amazon']['get_playback_resources_url'] % contentIdentifier
        r = Request(url, None, {'Cookie': self.config['amazon']['cookie']})
        data = json.loads(urlopen(r).read().decode('utf-8'))
        title = None
        season = None
        episode = data['catalogMetadata']['catalog']['episodeNumber']
        for f in data['catalogMetadata']['family']['tvAncestors']:
            if f['catalog']['type'] == 'SEASON':
                season = f['catalog']['seasonNumber']
            elif f['catalog']['type'] == 'SHOW':
                title = f['catalog']['title'].replace("[OV/OmU]", "").replace("[OV]", "").replace("[Ultra HD]", "")\
                    .replace("[dt./OV]", "").replace("(4K UHD)", "").strip()
        self.amazon_titles[contentIdentifier] = {'title': title, 'season': season, 'episode': episode}
        return title, season, episode

    def request_now_playing_description(self):
        msg = create(ProtocolMessage.PLAYBACK_QUEUE_REQUEST_MESSAGE)
        req = msg.inner()
        req.location = 0
        req.length = 1
        req.includeInfo = True
        resp = asyncio.run(self.protocol.send_and_receive(msg))
        self.now_playing_description = resp.inner().playbackQueue.contentItems[0].info

    @staticmethod
    def on_trakt_token_refreshed(response):
        pickle.dump(response, open('data/trakt.auth', 'wb'))
