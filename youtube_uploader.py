from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from time import sleep
from threading import Thread
from tzlocal import get_localzone
import pytz
import os
import pickle
import json
import datetime
import logging


class youtube_uploader():

    def __init__(self, parent, jsonfile, youtube_args, sort=True):
        self.parent = parent
        self.logger = logging.getLogger(f'vodloader.{self.parent.channel}.uploader')
        self.end = False
        self.pause = False
        self.sort = sort
        self.jsonfile = jsonfile
        self.youtube_args = youtube_args
        self.youtube = self.setup_youtube(jsonfile)
        self.queue = []
        self.upload_process = Thread(target=self.upload_loop, args=(), daemon=True)
        self.upload_process.start()

    def stop(self):
        self.end = True

    def setup_youtube(self, jsonfile, scopes=['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube']):
        self.logger.info(f'Building YouTube flow for {self.parent.channel}')
        api_name='youtube'
        api_version = 'v3'
        pickle_dir = os.path.join(os.path.dirname(__file__), 'pickles')
        if not os.path.exists(pickle_dir):
            self.logger.info(f'Creating pickle directory')
            os.mkdir(pickle_dir)
        pickle_file = os.path.join(pickle_dir, f'token_{self.parent.channel}.pickle')
        creds = None
        if os.path.exists(pickle_file):
            with open(pickle_file, 'rb') as token:
                creds = pickle.load(token)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                self.logger.info(f'YouTube credential pickle file for {self.parent.channel} is expired. Attempting to refresh now')
                creds.refresh(Request())
            else:
                print(f'Please log into the YouTube account that will host the vods of {self.parent.channel} below')
                flow = InstalledAppFlow.from_client_secrets_file(jsonfile, scopes)
                creds = flow.run_console()
            with open(pickle_file, 'wb') as token:
                pickle.dump(creds, token)
                self.logger.info(f'YouTube credential pickle file for {self.parent.channel} has been written to {pickle_file}')
        else:
            self.logger.info(f'YouTube credential pickle file for {self.parent.channel} found!')
        return build(api_name, api_version, credentials=creds)

    def upload_loop(self):
        while True:
            if len(self.queue) > 0:
                try:
                    self.upload_video(*self.queue[0])
                    del self.queue[0]
                except YouTubeOverQuota as e:
                    self.wait_for_quota()
            else: sleep(1)
            if self.end: break

    def upload_video(self, path, body, id, keep=False, chunk_size=4194304, retry=3):
        self.logger.info(f'Uploading file {path} to YouTube account for {self.parent.channel}')
        uploaded = False
        attempts = 0
        response = None
        while uploaded == False:
            media = MediaFileUpload(path, mimetype='video/mpegts', chunksize=chunk_size, resumable=True)
            upload = self.youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
            try:
                response = upload.execute()
                self.logger.debug(response)
                uploaded = response['status']['uploadStatus'] == 'uploaded'
            except HttpError as e:
                self.check_over_quota(e)
            except (BrokenPipeError, ConnectionResetError) as e:
                self.logger.error(e)
            if not uploaded:
                attempts += 1
            if attempts >= retry:
                self.logger.error(f'Number of retry attempts exceeded for {path}')
                break
        if response and 'id' in response:
            self.logger.info(f'Finished uploading {path} to https://youtube.com/watch?v={response["id"]}')
            if self.youtube_args['playlistId']:
                self.add_video_to_playlist(response["id"], self.youtube_args['playlistId'])
                if self.sort:
                    self.sort_playlist(self.youtube_args['playlistId'])
            self.parent.status[id] = True
            self.parent.status.save()
            if not keep: os.remove(path)
        else:
            self.logger.info(f'Could not parse a video ID from uploading {path}')
    
    def wait_for_quota(self):
        self.pause = True
        now = datetime.datetime.now()
        until = now + datetime.timedelta(days=1)
        until = until - datetime.timedelta(microseconds=until.microsecond, seconds=until.second, minutes=until.minute, hours=until.hour)
        until = pytz.timezone('US/Pacific').localize(until)
        now = get_localzone().localize(now)
        wait = until - now
        if wait.days > 0:
            wait = wait - datetime.timedelta(days=wait.days)
        self.logger.error(f'YouTube upload quota has been exceeded, waiting for reset at Midnight Pacific Time in {wait.seconds} seconds')
        sleep(wait.seconds + 15)
        self.pause = False

    def get_playlist_items(self, playlist_id):
        items = []
        npt = ""
        i = 1
        while True:
            request = self.youtube.playlistItems().list(
                part="snippet",
                maxResults=50,
                pageToken=npt,
                playlistId=playlist_id
            )
            try:
                response = request.execute()
            except HttpError as e:
                self.check_over_quota(e)
            self.logger.debug(f'Retrieved page {i} from playlist {playlist_id}')
            items.extend(response['items'])
            if 'nextPageToken' in response:
                npt = response['nextPageToken']
                i += 1
            else:
                break
        return items
    
    def get_videos_from_playlist_items(self, playlist_items):
        videos = []
        max_results = 50
        length = len(playlist_items)
        i = 0
        while i * max_results < length:
            top = max_results * (i + 1)
            if top > length: top = length
            ids = ",".join([x['snippet']['resourceId']['videoId'] for x in playlist_items[max_results*i:top]])
            request = self.youtube.videos().list(
                part="snippet",
                id=ids
            )
            try:
                response = request.execute()
            except HttpError as e:
                self.check_over_quota(e)
            self.logger.debug(f'Retrieved video info for videos: {ids}')
            videos.extend(response['items'])
            i += 1
        for video in videos:
            video['tvid'], video['part'] = self.get_tvid_from_yt_video(video)
        return videos
    
    def get_playlist_videos(self, playlist_id):
        return self.get_videos_from_playlist_items(self.get_playlist_items(playlist_id))
    
    def get_channel_videos(self):
        request = self.youtube.channels().list(part="contentDetails", mine=True)
        try:
            r = request.execute()
            self.logger.debug('Retrieved channel upload playlist')
            uploads = r['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        except HttpError as e:
            self.check_over_quota(e)
        return self.get_playlist_videos(uploads)
    
    @staticmethod
    def get_tvid_from_yt_video(item):
        if 'tags' in item['snippet']:
            tvid = None
            for tag in item['snippet']['tags']:
                if tag[:5] == 'tvid:':
                    tvid = tag[5:]
            if tvid:
                tvid = tvid.split('p', 1)
                id = int(tvid[0])
                if len(tvid) > 1: part = int(tvid[1])
                else: part = None
                return id, part
            else: return None, None
        else: return None, None

    def add_video_to_playlist(self, video_id, playlist_id, pos=-1):
        if pos == -1:
            pos = len(self.get_playlist_items(playlist_id))
        request = self.youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "position": pos,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id
                    }
                }
            }
        )
        try:
            r = request.execute()
            self.logger.debug(f'Added video {video_id} to playlist {playlist_id}')
            return r
        except HttpError as e:
            self.check_over_quota(e)
    
    def set_video_playlist_pos(self, video_id, playlist_item_id, playlist_id, pos):
        request = self.youtube.playlistItems().update(
            part="snippet",
            body={
                "id": playlist_item_id,
                "snippet": {
                    "playlistId": playlist_id,
                    "position": pos,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id
                    }
                }
            }
        )
        try:
            r = request.execute()
            self.logger.debug(f'Moved item {video_id} to position {pos} in playlist {playlist_id}')
            return r
        except HttpError as e:
            self.check_over_quota(e)

    def sort_playlist(self, playlist_id, reverse=False):
        self.logger.debug(f'Sorting playlist {playlist_id} according to tvid and part')
        playlist_items = self.get_playlist_items(playlist_id)
        videos = self.get_videos_from_playlist_items(playlist_items)
        unsortable = []
        for video in videos:
            if video['tvid'] == None:
                unsortable.append(video['id'])
        if unsortable != []:
            self.logger.error(f"There were videos found in the specified playlist to be sorted without a valid tvid tag. As such this playlist cannot be reliably sorted. The videos specified are: {','.join(unsortable)}")
            return
        try:
            videos.sort(reverse=reverse, key=lambda x: (x['tvid'], x['part']))
        except TypeError as e:
            dupes = {}
            invalid = []
            for video in videos:
                if video['tvid'] in dupes:
                    dupes['tvid'].append(video)
                else:
                    dupes['tvid'] = [video]
            for tvid in dupes:
                if len(dupes[tvid]) > 1:
                    for video in dupes[tvid]:
                        if video['part'] == None:
                            invalid.append(video['id'])
            self.logger.error(f"There were videos found in the specified playlist to be sorted that has duplicate tvid tags, but no part specified. As such this playlist cannot be reliably sorted. The videos specified are: {','.join(invalid)}")
            return
        i = 0
        while i < len(videos):
            if videos[i]['id'] != playlist_items[i]['snippet']['resourceId']['videoId']:
                j = i + 1
                while videos[i]['id'] != playlist_items[j]['snippet']['resourceId']['videoId'] and j <= len(videos): j+=1
                if j < len(videos):
                    self.set_video_playlist_pos(playlist_items[j]['snippet']['resourceId']['videoId'], playlist_items[j]['id'], playlist_id, i)
                    playlist_items.insert(i, playlist_items.pop(j))
                else:
                    self.logger.error('An error has occured while sorting the playlist')
                    return
            i+=1
    
    def check_over_quota(self, e: HttpError):
        c = json.loads(e.content)
        if c['error']['errors'][0]['domain'] == 'youtube.quota' and c['error']['errors'][0]['reason'] == 'quotaExceeded':
            self.logger.error(f'YouTube client quota has been exceeded!')
            raise YouTubeOverQuota
        else:
            self.logger.error(e.resp)
            self.logger.error(e.content)
    
class YouTubeOverQuota(Exception):
    """ called when youtube upload quota is exceeded """
    pass