import unicodedata
import re
from datetime import datetime
from urllib.parse import urlparse

from utils.models import *
from .qobuz_api import Qobuz

module_information = ModuleInformation(
    service_name = 'Qobuz',
    module_supported_modes = ModuleModes.download | ModuleModes.credits,
    global_settings = {'app_id': '', 'app_secret': '', 'quality_format': '{bit_depth}B-{sample_rate}kHz','user_id':'', 'auth_token': '', 'extra_accounts': []},
    netlocation_constant = 'qobuz',
    login_behaviour = ManualEnum.manual,
    url_decoding = ManualEnum.manual, # 🚀 ADD THIS
    url_constants={
        'track': DownloadTypeEnum.track,
        'album': DownloadTypeEnum.album,
        'playlist': DownloadTypeEnum.playlist,
        'artist': DownloadTypeEnum.artist,
        'interpreter': DownloadTypeEnum.artist,
        'label': DownloadTypeEnum.artist,
        'award': DownloadTypeEnum.artist
    },
    test_url = 'https://open.qobuz.com/track/52151405'
)


class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        settings = module_controller.module_settings
        self.module_controller = module_controller
        self.sessions = []
        
        # 1. Load Primary Session
        primary_session = Qobuz(settings['app_id'], settings['app_secret'], settings['auth_token'], module_controller.module_error)
        try:
            primary_session.check_token()
            self.sessions.append(primary_session)
        except Exception as e:
            print(f"Primary Qobuz token failed: {e}")

        # 2. Load Extra Accounts (AppID:Token or just Token)
        extra_accs = settings.get('extra_accounts', [])
        if isinstance(extra_accs, str):
            extra_accs = [x.strip() for x in extra_accs.split(',')]
            
        for acc in extra_accs:
            try:
                # If the string contains a colon, split it into app_id and token!
                if ':' in acc:
                    parts = acc.split(':', 1)
                    extra_session = Qobuz(parts[0].strip(), settings['app_secret'], parts[1].strip(), module_controller.module_error)
                else:
                    extra_session = Qobuz(settings['app_id'], settings['app_secret'], acc.strip(), module_controller.module_error)
                
                extra_session.check_token()
                self.sessions.append(extra_session)
            except Exception as e:
                print(f"Extra account failed: {e}")

        if not self.sessions:
            raise Exception("No valid Qobuz accounts loaded!")
            
        self.session = self.sessions[0] # Fallback compatibility

        # 5 = 320 kbps MP3, 6 = 16-bit FLAC, 7 = 24-bit / =< 96kHz FLAC, 27 =< 192 kHz FLAC
        self.quality_parse = {
            QualityEnum.MINIMUM: 5,
            QualityEnum.LOW: 5,
            QualityEnum.MEDIUM: 5,
            QualityEnum.HIGH: 5,
            QualityEnum.LOSSLESS: 6,
            QualityEnum.HIFI: 27
        }
        self.quality_tier = module_controller.orpheus_options.quality_tier
        self.quality_format = settings.get('quality_format')

    def custom_url_parse(self, link: str):
        # 1. Clean the link of query parameters or trailing slashes
        link = link.split('?')[0].rstrip('/')
        
        # 2. 🚀 Updated Regex to skip descriptive text and grab the ID at the very end
        match = re.search(r'(track|album|playlist|artist|interpreter|label|award)/(?:.*?/)?([A-Za-z0-9]+)$', link)
        if not match:
            raise Exception('Unsupported Qobuz URL')
            
        media_type_str = match.group(1)
        media_id = match.group(2)
        
        type_map = {
            'track': DownloadTypeEnum.track,
            'album': DownloadTypeEnum.album,
            'playlist': DownloadTypeEnum.playlist,
            'artist': DownloadTypeEnum.artist,
            'interpreter': DownloadTypeEnum.artist,
            'label': DownloadTypeEnum.artist,
            'award': DownloadTypeEnum.artist
        }
        
        # Tag label & award IDs so the scraper knows which endpoint to hit
        if media_type_str == 'label':
            media_id = f"label_{media_id}"
        elif media_type_str == 'award':
            # 🚀 CHANGE: Pretend it's a label so the core bot bypasses the filter!
            media_id = f"label_award_{media_id}"
            
        return MediaIdentification(media_type=type_map.get(media_type_str), media_id=media_id)
        
    def _get_data(self, function_name, *args, **kwargs):
        last_error = None
        for session in self.sessions:
            try:
                func = getattr(session, function_name)
                result = func(*args, **kwargs)
                
                if function_name == 'get_track':
                    if not result.get('streamable', True):
                         raise Exception("Track not streamable on this region")
                         
                if function_name == 'get_file_url':
                    if not result.get('url'):
                        raise Exception("No file URL returned (Region Lock?)")

                return result
            except Exception as e:
                last_error = e
                continue
        
        if last_error:
            raise last_error
        raise Exception("All accounts failed to fetch data")


    def get_track_info(self, track_id, quality_tier: QualityEnum, codec_options: CodecOptions, data={}):
        track_data = data[track_id] if track_id in data else self._get_data('get_track', track_id)
        album_data = track_data['album']

        quality_tier = self.quality_parse[quality_tier]

        main_artist = track_data.get('performer', album_data['artist'])
        artists = [
            unicodedata.normalize('NFKD', main_artist['name'])
            .encode('ascii', 'ignore')
            .decode('utf-8')
        ]

        # Filter MainArtist and FeaturedArtist from performers
        if track_data.get('performers'):
            performers = []
            for credit in track_data['performers'].split(' - '):
                contributor_role = credit.split(', ')[1:]
                contributor_name = credit.split(', ')[0]

                for contributor in ['MainArtist', 'FeaturedArtist', 'Artist']:
                    if contributor in contributor_role:
                        if contributor_name not in artists:
                            artists.append(contributor_name)
                        contributor_role.remove(contributor)

                if not contributor_role:
                    continue
                performers.append(f"{contributor_name}, {', '.join(contributor_role)}")
            track_data['performers'] = ' - '.join(performers)
        artists[0] = main_artist['name']

        tags = Tags(
            album_artist = album_data['artist']['name'],
            composer = track_data['composer']['name'] if 'composer' in track_data else None,
            release_date = album_data.get('release_date_original'),
            track_number = track_data['track_number'],
            total_tracks = album_data['tracks_count'],
            disc_number = track_data['media_number'],
            total_discs = album_data['media_count'],
            isrc = track_data.get('isrc'),
            upc = album_data.get('upc'),
            label = album_data.get('label').get('name') if album_data.get('label') else None,
            copyright = album_data.get('copyright'),
            genres = [album_data['genre']['name']],
        )

        stream_data = self._get_data('get_file_url', track_id, quality_tier)
        # uncompressed PCM bitrate calculation, not quite accurate for FLACs due to the up to 60% size improvement
        bitrate = 320
        if stream_data.get('format_id') in {6, 7, 27}:
            bitrate = int((stream_data['sampling_rate'] * 1000 * stream_data['bit_depth'] * 2) // 1000)
        elif not stream_data.get('format_id'):
            bitrate = stream_data.get('format_id')

        # track and album title fix to include version tag
        track_name = f"{track_data.get('work')} - " if track_data.get('work') else ""
        track_name += track_data.get('title').rstrip()
        track_name += f' ({track_data.get("version")})' if track_data.get("version") else ''

        album_name = album_data.get('title').rstrip()
        album_name += f' ({album_data.get("version")})' if album_data.get("version") else ''

        return TrackInfo(
            name = track_name,
            album_id = album_data['id'],
            album = album_name,
            artists = artists,
            artist_id = main_artist['id'],
            bit_depth = stream_data['bit_depth'],
            bitrate = bitrate,
            sample_rate = stream_data['sampling_rate'],
            release_year = int(album_data['release_date_original'].split('-')[0]),
            explicit = track_data['parental_warning'],
            cover_url = album_data['image']['large'].split('_')[0] + '_org.jpg',
            tags = tags,
            codec = CodecEnum.FLAC if stream_data.get('format_id') in {6, 7, 27} else CodecEnum.NONE if not stream_data.get('format_id') else CodecEnum.MP3,
            duration = track_data.get('duration'),
            credits_extra_kwargs = {'data': {track_id: track_data}},
            download_extra_kwargs = {'url': stream_data.get('url')},
            error=f'Track "{track_data["title"]}" is not streamable!' if not track_data['streamable'] else None
        )

    def get_track_download(self, url):
        return TrackDownloadInfo(download_type=DownloadEnum.URL, file_url=url)

    def get_album_info(self, album_id):
        album_data = self._get_data('get_album', album_id)
        booklet_url = album_data['goodies'][0]['url'] if 'goodies' in album_data and len(album_data['goodies']) != 0 else None

        tracks, extra_kwargs = [], {}
        for track in album_data.pop('tracks')['items']:
            track_id = str(track['id'])
            tracks.append(track_id)
            track['album'] = album_data
            extra_kwargs[track_id] = track

        # get the wanted quality for an actual album quality_format string
        quality_tier = self.quality_parse[self.quality_tier]
        # TODO: Ignore sample_rate and bit_depth if album_data['hires'] is False?
        bit_depth = 24 if quality_tier == 27 and album_data['hires_streamable'] else 16
        sample_rate = album_data['maximum_sampling_rate'] if quality_tier == 27 and album_data[
            'hires_streamable'] else 44.1

        quality_tags = {
            'sample_rate': sample_rate,
            'bit_depth': bit_depth
        }

        # album title fix to include version tag
        album_name = album_data.get('title').rstrip()
        album_name += f' ({album_data.get("version")})' if album_data.get("version") else ''

        return AlbumInfo(
            name = album_name,
            artist = album_data['artist']['name'],
            artist_id = album_data['artist']['id'],
            tracks = tracks,
            release_year = int(album_data['release_date_original'].split('-')[0]),
            explicit = album_data['parental_warning'],
            quality = self.quality_format.format(**quality_tags) if self.quality_format != '' else None,
            description = album_data.get('description'),
            cover_url = album_data['image']['large'].split('_')[0] + '_org.jpg',
            all_track_cover_jpg_url = album_data['image']['large'],
            upc = album_data.get('upc'),
            duration = album_data.get('duration'),
            booklet_url = booklet_url,
            track_extra_kwargs = {'data': extra_kwargs}
        )

    def get_playlist_info(self, playlist_id):
        playlist_data = self._get_data('get_playlist', playlist_id)

        tracks, extra_kwargs = [], {}
        for track in playlist_data['tracks']['items']:
            track_id = str(track['id'])
            extra_kwargs[track_id] = track
            tracks.append(track_id)

        return PlaylistInfo(
            name = playlist_data['name'],
            creator = playlist_data['owner']['name'],
            creator_id = playlist_data['owner']['id'],
            release_year = datetime.utcfromtimestamp(playlist_data['created_at']).strftime('%Y'),
            description = playlist_data.get('description'),
            duration = playlist_data.get('duration'),
            tracks = tracks,
            track_extra_kwargs = {'data': extra_kwargs}
        )

    def get_label_info(self, label_id, get_credited_albums=False):
        albums = []
        label_name = f'Label {label_id}'
        
        for session in self.sessions:
            try:
                label_data = session.get_label(label_id)
                if 'name' in label_data: label_name = label_data['name']
                if 'albums' in label_data and 'items' in label_data['albums']:
                    for album in label_data['albums']['items']:
                        aid = str(album['id'])
                        if aid not in albums: albums.append(aid)
            except: continue
                
        if not albums: raise Exception("No albums found for this label in any region.")
        return ArtistInfo(name=label_name, albums=albums)

    def get_award_info(self, award_id, get_credited_albums=False):
        albums = []
        award_name = f'Award {award_id}'
        print(f"[DEBUG] Starting fetch for Award ID: {award_id} across {len(self.sessions)} sessions", flush=True)
        
        for idx, session in enumerate(self.sessions):
            try:
                print(f"[DEBUG] [Session {idx+1}/{len(self.sessions)}] Attempting to fetch award data...", flush=True)
                award_data = session.get_award(award_id)
                if 'name' in award_data: award_name = award_data['name']
                if 'albums' in award_data and 'items' in award_data['albums']:
                    for album in award_data['albums']['items']:
                        aid = str(album['id'])
                        if aid not in albums: albums.append(aid)
                
                print(f"[DEBUG] Success on session {idx+1}. Total unique albums found so far: {len(albums)}", flush=True)
                
                # NOTE: If you don't need to cross-reference every region, uncomment the break below
                # break 
            except Exception as e: 
                print(f"[DEBUG] Failed fetching Award {award_id} on session {idx+1}: {e}", flush=True)
                continue
                
        if not albums: raise Exception("No albums found for this award in any region.")
        return ArtistInfo(name=award_name, albums=albums)

    def get_artist_info(self, artist_id, get_credited_albums):
        # Route Label & Award requests coming from the custom parser
        # 🚀 CHANGE: Catch the fake 'label_award_' tag before the regular label tag
        if str(artist_id).startswith('label_award_'):
            return self.get_award_info(artist_id.replace('label_award_', ''), get_credited_albums)
        elif str(artist_id).startswith('label_'):
            return self.get_label_info(artist_id.replace('label_', ''), get_credited_albums)
            
        albums = []
        artist_name = "Unknown Artist"
        
        for session in self.sessions:
            try:
                artist_data = session.get_artist(artist_id)
                if 'name' in artist_data: artist_name = artist_data['name']
                if 'albums' in artist_data and 'items' in artist_data['albums']:
                    for album in artist_data['albums']['items']:
                        aid = str(album['id'])
                        if aid not in albums: albums.append(aid)
            except: continue

        if not albums: raise Exception("No albums found for this artist in any region.")
        return ArtistInfo(name=artist_name, albums=albums)
        
    def get_track_credits(self, track_id, data=None):
        track_data = data[track_id] if track_id in data else self._get_data('get_track', track_id)
        track_contributors = track_data.get('performers')

        # Credits look like: {name}, {type1}, {type2} - {name2}, {type2}
        credits_dict = {}
        if track_contributors:
            for credit in track_contributors.split(' - '):
                contributor_role = credit.split(', ')[1:]
                contributor_name = credit.split(', ')[0]

                for role in contributor_role:
                    # Check if the dict contains no list, create one
                    if role not in credits_dict:
                        credits_dict[role] = []
                    # Now add the name to the type list
                    credits_dict[role].append(contributor_name)

        # Convert the dictionary back to a list of CreditsInfo
        return [CreditsInfo(k, v) for k, v in credits_dict.items()]

    def search(self, query_type: DownloadTypeEnum, query, track_info: TrackInfo = None, limit: int = 10):
        results = {}
        if track_info and track_info.tags.isrc:
            results = self._get_data('search', query_type.name, track_info.tags.isrc, limit)
        if not results:
            results = self._get_data('search', query_type.name, query, limit)

        items = []
        for i in results[query_type.name + 's']['items']:
            duration = None
            if query_type is DownloadTypeEnum.artist:
                artists = None
                year = None
            elif query_type is DownloadTypeEnum.playlist:
                artists = [i['owner']['name']]
                year = datetime.utcfromtimestamp(i['created_at']).strftime('%Y')
                duration = i['duration']
            elif query_type is DownloadTypeEnum.track:
                artists = [i['performer']['name']]
                year = int(i['album']['release_date_original'].split('-')[0])
                duration = i['duration']
            elif query_type is DownloadTypeEnum.album:
                artists = [i['artist']['name']]
                year = int(i['release_date_original'].split('-')[0])
                duration = i['duration']
            else:
                raise Exception('Query type is invalid')
            name = i.get('name') or i.get('title')
            name += f" ({i.get('version')})" if i.get('version') else ''
            item = SearchResult(
                name = name,
                artists = artists,
                year = year,
                result_id = str(i['id']),
                explicit = bool(i.get('parental_warning')),
                additional = [f'{i["maximum_sampling_rate"]}kHz/{i["maximum_bit_depth"]}bit'] if "maximum_sampling_rate" in i else None,
                duration = duration,
                extra_kwargs = {'data': {str(i['id']): i}} if query_type is DownloadTypeEnum.track else {}
            )

            items.append(item)

        return items
