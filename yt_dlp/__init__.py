#!/usr/bin/env python3
f'You are using an unsupported version of Python. Only Python versions 3.6 and above are supported by yt-dlp'  # noqa: F541

__license__ = 'Public Domain'

import itertools
import os
import random
import re
import sys

from .options import parseOpts
from .compat import (
    compat_getpass,
    compat_os_name,
    compat_shlex_quote,
)
from .cookies import SUPPORTED_BROWSERS, SUPPORTED_KEYRINGS
from .utils import (
    DateRange,
    decodeOption,
    DownloadCancelled,
    DownloadError,
    expand_path,
    float_or_none,
    GeoUtils,
    int_or_none,
    match_filter_func,
    NO_DEFAULT,
    parse_duration,
    preferredencoding,
    read_batch_urls,
    render_table,
    SameFileError,
    setproctitle,
    std_headers,
    traverse_obj,
    write_string,
)
from .update import run_update
from .downloader import FileDownloader
from .extractor import gen_extractors, list_extractors
from .extractor.common import InfoExtractor
from .extractor.adobepass import MSO_INFO
from .postprocessor import (
    FFmpegExtractAudioPP,
    FFmpegSubtitlesConvertorPP,
    FFmpegThumbnailsConvertorPP,
    FFmpegVideoConvertorPP,
    FFmpegVideoRemuxerPP,
    MetadataFromFieldPP,
    MetadataParserPP,
)
from .YoutubeDL import YoutubeDL


def get_urls(urls, batchfile, verbose):
    # Batch file verification
    batch_urls = []
    if batchfile is not None:
        try:
            if batchfile == '-':
                write_string('Reading URLs from stdin - EOF (%s) to end:\n' % (
                    'Ctrl+Z' if compat_os_name == 'nt' else 'Ctrl+D'))
                batchfd = sys.stdin
            else:
                batchfd = open(
                    expand_path(batchfile), encoding='utf-8', errors='ignore')
            batch_urls = read_batch_urls(batchfd)
            if verbose:
                write_string('[debug] Batch file urls: ' + repr(batch_urls) + '\n')
        except OSError:
            sys.exit('ERROR: batch file %s could not be read' % batchfile)
    _enc = preferredencoding()
    return [
        url.strip().decode(_enc, 'ignore') if isinstance(url, bytes) else url.strip()
        for url in batch_urls + urls]


def print_extractor_information(opts, urls):
    if opts.list_extractors:
        for ie in list_extractors(opts.age_limit):
            write_string(ie.IE_NAME + (' (CURRENTLY BROKEN)' if not ie.working() else '') + '\n', out=sys.stdout)
            matchedUrls = [url for url in urls if ie.suitable(url)]
            for mu in matchedUrls:
                write_string('  ' + mu + '\n', out=sys.stdout)
    elif opts.list_extractor_descriptions:
        for ie in list_extractors(opts.age_limit):
            if not ie.working():
                continue
            if ie.IE_DESC is False:
                continue
            desc = ie.IE_DESC or ie.IE_NAME
            if getattr(ie, 'SEARCH_KEY', None) is not None:
                _SEARCHES = ('cute kittens', 'slithering pythons', 'falling cat', 'angry poodle', 'purple fish', 'running tortoise', 'sleeping bunny', 'burping cow')
                _COUNTS = ('', '5', '10', 'all')
                desc += f'; "{ie.SEARCH_KEY}:" prefix (Example: "{ie.SEARCH_KEY}{random.choice(_COUNTS)}:{random.choice(_SEARCHES)}")'
            write_string(desc + '\n', out=sys.stdout)
    elif opts.ap_list_mso:
        table = [[mso_id, mso_info['name']] for mso_id, mso_info in MSO_INFO.items()]
        write_string('Supported TV Providers:\n' + render_table(['mso', 'mso name'], table) + '\n', out=sys.stdout)
    else:
        return False
    return True


def set_compat_opts(opts):
    def _unused_compat_opt(name):
        if name not in opts.compat_opts:
            return False
        opts.compat_opts.discard(name)
        opts.compat_opts.update(['*%s' % name])
        return True

    def set_default_compat(compat_name, opt_name, default=True, remove_compat=True):
        attr = getattr(opts, opt_name)
        if compat_name in opts.compat_opts:
            if attr is None:
                setattr(opts, opt_name, not default)
                return True
            else:
                if remove_compat:
                    _unused_compat_opt(compat_name)
                return False
        elif attr is None:
            setattr(opts, opt_name, default)
        return None

    set_default_compat('abort-on-error', 'ignoreerrors', 'only_download')
    set_default_compat('no-playlist-metafiles', 'allow_playlist_files')
    set_default_compat('no-clean-infojson', 'clean_infojson')
    if 'no-attach-info-json' in opts.compat_opts:
        if opts.embed_infojson:
            _unused_compat_opt('no-attach-info-json')
        else:
            opts.embed_infojson = False
    if 'format-sort' in opts.compat_opts:
        opts.format_sort.extend(InfoExtractor.FormatSort.ytdl_default)
    _video_multistreams_set = set_default_compat('multistreams', 'allow_multiple_video_streams', False, remove_compat=False)
    _audio_multistreams_set = set_default_compat('multistreams', 'allow_multiple_audio_streams', False, remove_compat=False)
    if _video_multistreams_set is False and _audio_multistreams_set is False:
        _unused_compat_opt('multistreams')
    if 'filename' in opts.compat_opts:
        if opts.outtmpl.get('default') is None:
            opts.outtmpl.update({'default': '%(title)s-%(id)s.%(ext)s'})
        else:
            _unused_compat_opt('filename')


def validate_options(opts):
    def validate(cndn, name, value=None, msg=None):
        if cndn:
            return True
        raise ValueError((msg or 'invalid {name} "{value}" given').format(name=name, value=value))

    def validate_in(name, value, items, msg=None):
        return validate(value is None or value in items, name, value, msg)

    def validate_regex(name, value, regex):
        return validate(value is None or re.match(regex, value), name, value)

    def validate_positive(name, value, strict=False):
        return validate(value is None or value > 0 or (not strict and value == 0),
                        name, value, '{name} "{value}" must be positive' + ('' if strict else ' or 0'))

    def validate_minmax(min_val, max_val, min_name, max_name=None):
        if max_val is None or min_val is None or max_val >= min_val:
            return
        if not max_name:
            min_name, max_name = f'min {min_name}', f'max {min_name}'
        raise ValueError(f'{max_name} "{max_val}" must be must be greater than or equal to {min_name} "{min_val}"')

    # Usernames and passwords
    validate(not opts.usenetrc or (opts.username is None and opts.password is None),
             '.netrc', msg='using {name} conflicts with giving username/password')
    validate(opts.password is None or opts.username is not None, 'account username', msg='{name} missing')
    validate(opts.ap_password is None or opts.ap_username is not None,
             'TV Provider account username', msg='{name} missing')
    validate_in('TV Provider', opts.ap_mso, MSO_INFO,
                'Unsupported {name} "{value}", use --ap-list-mso to get a list of supported TV Providers')

    # Numbers
    validate_positive('autonumber start', opts.autonumber_start)
    validate_positive('autonumber size', opts.autonumber_size, True)
    validate_positive('concurrent fragments', opts.concurrent_fragment_downloads, True)
    validate_positive('playlist start', opts.playliststart, True)
    if opts.playlistend != -1:
        validate_minmax(opts.playliststart, opts.playlistend, 'playlist start', 'playlist end')

    # Time ranges
    validate_positive('subtitles sleep interval', opts.sleep_interval_subtitles)
    validate_positive('requests sleep interval', opts.sleep_interval_requests)
    validate_positive('sleep interval', opts.sleep_interval)
    validate_positive('max sleep interval', opts.max_sleep_interval)
    if opts.sleep_interval is None:
        validate(
            opts.max_sleep_interval is None, 'min sleep interval',
            msg='{name} must be specified; use --min-sleep-interval')
    elif opts.max_sleep_interval is None:
        opts.max_sleep_interval = opts.sleep_interval
    else:
        validate_minmax(opts.sleep_interval, opts.max_sleep_interval, 'sleep interval')

    if opts.wait_for_video is not None:
        min_wait, max_wait, *_ = map(parse_duration, opts.wait_for_video.split('-', 1) + [None])
        validate(min_wait is not None and not (max_wait is None and '-' in opts.wait_for_video),
                 'time range to wait for video', opts.wait_for_video)
        validate_minmax(min_wait, max_wait, 'time range to wait for video')
        opts.wait_for_video = (min_wait, max_wait)

    # Format sort
    for f in opts.format_sort:
        validate_regex('format sorting', f, InfoExtractor.FormatSort.regex)

    # Postprocessor formats
    validate_in('audio format', opts.audioformat, ['best'] + list(FFmpegExtractAudioPP.SUPPORTED_EXTS))
    validate_in('subtitle format', opts.convertsubtitles, FFmpegSubtitlesConvertorPP.SUPPORTED_EXTS)
    validate_in('thumbnail format', opts.convertthumbnails, FFmpegThumbnailsConvertorPP.SUPPORTED_EXTS)
    if opts.recodevideo is not None:
        opts.recodevideo = opts.recodevideo.replace(' ', '')
        validate_regex('video recode format', opts.recodevideo, FFmpegVideoConvertorPP.FORMAT_RE)
    if opts.remuxvideo is not None:
        opts.remuxvideo = opts.remuxvideo.replace(' ', '')
        validate_regex('video remux format', opts.remuxvideo, FFmpegVideoRemuxerPP.FORMAT_RE)
    if opts.audioquality:
        opts.audioquality = opts.audioquality.strip('k').strip('K')
        # int_or_none prevents inf, nan
        validate_positive('audio quality', int_or_none(float_or_none(opts.audioquality), default=0))

    # Retries
    def parse_retries(name, value):
        if value is None:
            return None
        elif value in ('inf', 'infinite'):
            return float('inf')
        try:
            return int(value)
        except (TypeError, ValueError):
            validate(False, f'{name} retry count', value)

    opts.retries = parse_retries('download', opts.retries)
    opts.fragment_retries = parse_retries('fragment', opts.fragment_retries)
    opts.extractor_retries = parse_retries('extractor', opts.extractor_retries)
    opts.file_access_retries = parse_retries('file access', opts.file_access_retries)

    # Bytes
    def parse_bytes(name, value):
        if value is None:
            return None
        numeric_limit = FileDownloader.parse_bytes(value)
        validate(numeric_limit is not None, 'rate limit', value)
        return numeric_limit

    opts.ratelimit = parse_bytes('rate limit', opts.ratelimit)
    opts.throttledratelimit = parse_bytes('throttled rate limit', opts.throttledratelimit)
    opts.min_filesize = parse_bytes('min filesize', opts.min_filesize)
    opts.max_filesize = parse_bytes('max filesize', opts.max_filesize)
    opts.buffersize = parse_bytes('buffer size', opts.buffersize)
    opts.http_chunk_size = parse_bytes('http chunk size', opts.http_chunk_size)

    # Output templates
    def validate_outtmpl(tmpl, msg):
        err = YoutubeDL.validate_outtmpl(tmpl)
        if err:
            raise ValueError(f'invalid {msg} "{tmpl}": {err}')

    for k, tmpl in opts.outtmpl.items():
        validate_outtmpl(tmpl, f'{k} output template')
    for type_, tmpl_list in opts.forceprint.items():
        for tmpl in tmpl_list:
            validate_outtmpl(tmpl, f'{type_} print template')
    for type_, tmpl_list in opts.print_to_file.items():
        for tmpl, file in tmpl_list:
            validate_outtmpl(tmpl, f'{type_} print to file template')
            validate_outtmpl(file, f'{type_} print to file filename')
    validate_outtmpl(opts.sponsorblock_chapter_title, 'SponsorBlock chapter title')
    for k, tmpl in opts.progress_template.items():
        k = f'{k[:-6]} console title' if '-title' in k else f'{k} progress'
        validate_outtmpl(tmpl, f'{k} template')

    outtmpl_default = opts.outtmpl.get('default')
    if outtmpl_default == '':
        opts.skip_download = None
        del opts.outtmpl['default']
    if outtmpl_default and not os.path.splitext(outtmpl_default)[1] and opts.extractaudio:
        raise ValueError(
            'Cannot download a video and extract audio into the same file! '
            f'Use "{outtmpl_default}.%(ext)s" instead of "{outtmpl_default}" as the output template')

    # Remove chapters
    remove_chapters_patterns, opts.remove_ranges = [], []
    for regex in opts.remove_chapters or []:
        if regex.startswith('*'):
            dur = list(map(parse_duration, regex[1:].split('-')))
            if len(dur) == 2 and all(t is not None for t in dur):
                opts.remove_ranges.append(tuple(dur))
                continue
            raise ValueError(f'invalid --remove-chapters time range "{regex}". Must be of the form *start-end')
        try:
            remove_chapters_patterns.append(re.compile(regex))
        except re.error as err:
            raise ValueError(f'invalid --remove-chapters regex "{regex}" - {err}')
    opts.remove_chapters = remove_chapters_patterns

    # Cookies from browser
    if opts.cookiesfrombrowser:
        mobj = re.match(r'(?P<name>[^+:]+)(\s*\+\s*(?P<keyring>[^:]+))?(\s*:(?P<profile>.+))?', opts.cookiesfrombrowser)
        if mobj is None:
            raise ValueError(f'invalid cookies from browser arguments: {opts.cookiesfrombrowser}')
        browser_name, keyring, profile = mobj.group('name', 'keyring', 'profile')
        browser_name = browser_name.lower()
        if browser_name not in SUPPORTED_BROWSERS:
            raise ValueError(f'unsupported browser specified for cookies: "{browser_name}". '
                             f'Supported browsers are: {", ".join(sorted(SUPPORTED_BROWSERS))}')
        if keyring is not None:
            keyring = keyring.upper()
            if keyring not in SUPPORTED_KEYRINGS:
                raise ValueError(f'unsupported keyring specified for cookies: "{keyring}". '
                                 f'Supported keyrings are: {", ".join(sorted(SUPPORTED_KEYRINGS))}')
        opts.cookiesfrombrowser = (browser_name, profile, keyring)

    # MetadataParser
    def metadataparser_actions(f):
        if isinstance(f, str):
            cmd = '--parse-metadata %s' % compat_shlex_quote(f)
            try:
                actions = [MetadataFromFieldPP.to_action(f)]
            except Exception as err:
                raise ValueError(f'{cmd} is invalid; {err}')
        else:
            cmd = '--replace-in-metadata %s' % ' '.join(map(compat_shlex_quote, f))
            actions = ((MetadataParserPP.Actions.REPLACE, x, *f[1:]) for x in f[0].split(','))

        for action in actions:
            try:
                MetadataParserPP.validate_action(*action)
            except Exception as err:
                raise ValueError(f'{cmd} is invalid; {err}')
            yield action

    parse_metadata = opts.parse_metadata or []
    if opts.metafromtitle is not None:
        parse_metadata.append('title:%s' % opts.metafromtitle)
    opts.parse_metadata = list(itertools.chain(*map(metadataparser_actions, parse_metadata)))

    # Other options
    geo_bypass_code = opts.geo_bypass_ip_block or opts.geo_bypass_country
    if geo_bypass_code is not None:
        try:
            GeoUtils.random_ipv4(geo_bypass_code)
        except Exception:
            raise ValueError('unsupported geo-bypass country or ip-block')

    opts.match_filter = match_filter_func(opts.match_filter)

    if opts.download_archive is not None:
        opts.download_archive = expand_path(opts.download_archive)

    if opts.user_agent is not None:
        opts.headers.setdefault('User-Agent', opts.user_agent)
    if opts.referer is not None:
        opts.headers.setdefault('Referer', opts.referer)

    if opts.no_sponsorblock:
        opts.sponsorblock_mark = opts.sponsorblock_remove = set()

    warnings, deprecation_warnings = [], []

    # Common mistake: -f best
    if opts.format == 'best':
        warnings.append('.\n         '.join((
            '"-f best" selects the best pre-merged format which is often not the best option',
            'To let yt-dlp download and merge the best available formats, simply do not pass any format selection',
            'If you know what you are doing and want only the best pre-merged format, use "-f b" instead to suppress this warning')))

    # --(postprocessor/downloader)-args without name
    def report_args_compat(name, value, key1, key2=None):
        if key1 in value and key2 not in value:
            warnings.append(f'{name} arguments given without specifying name. The arguments will be given to all {name}s')
            return True
        return False

    report_args_compat('external downloader', opts.external_downloader_args, 'default')
    if report_args_compat('post-processor', opts.postprocessor_args, 'default-compat', 'default'):
        opts.postprocessor_args['default'] = opts.postprocessor_args.pop('default-compat')
        opts.postprocessor_args.setdefault('sponskrub', [])

    def report_conflict(arg1, opt1, arg2='--allow-unplayable-formats', opt2='allow_unplayable_formats',
                        val1=NO_DEFAULT, val2=NO_DEFAULT, default=False):
        if val2 is NO_DEFAULT:
            val2 = getattr(opts, opt2)
        if not val2:
            return

        if val1 is NO_DEFAULT:
            val1 = getattr(opts, opt1)
        if val1:
            warnings.append(f'{arg1} is ignored since {arg2} was given')
        setattr(opts, opt1, default)

    # Conflicting options
    report_conflict('--dateafter', 'dateafter', '--date', 'date', default=None)
    report_conflict('--datebefore', 'datebefore', '--date', 'date', default=None)
    report_conflict('--exec-before-download', 'exec_before_dl_cmd', '"--exec before_dl:"', 'exec_cmd', opts.exec_cmd.get('before_dl'))
    report_conflict('--id', 'useid', '--output', 'outtmpl', val2=opts.outtmpl.get('default'))
    report_conflict('--remux-video', 'remuxvideo', '--recode-video', 'recodevideo')
    report_conflict('--sponskrub', 'sponskrub', '--remove-chapters', 'remove_chapters')
    report_conflict('--sponskrub', 'sponskrub', '--sponsorblock-mark', 'sponsorblock_mark')
    report_conflict('--sponskrub', 'sponskrub', '--sponsorblock-remove', 'sponsorblock_remove')
    report_conflict('--sponskrub-cut', 'sponskrub_cut', '--split-chapter', 'split_chapters', val1=opts.sponskrub and opts.sponskrub_cut)

    # Conflicts with --allow-unplayable-formats
    report_conflict('--add-metadata', 'addmetadata')
    report_conflict('--embed-chapters', 'addchapters')
    report_conflict('--embed-info-json', 'embed_infojson')
    report_conflict('--embed-subs', 'embedsubtitles')
    report_conflict('--embed-thumbnail', 'embedthumbnail')
    report_conflict('--extract-audio', 'extractaudio')
    report_conflict('--fixup', 'fixup', val1=(opts.fixup or '').lower() in ('', 'never', 'ignore'), default='never')
    report_conflict('--recode-video', 'recodevideo')
    report_conflict('--remove-chapters', 'remove_chapters', default=[])
    report_conflict('--remux-video', 'remuxvideo')
    report_conflict('--sponskrub', 'sponskrub')
    report_conflict('--sponsorblock-remove', 'sponsorblock_remove', default=set())
    report_conflict('--xattrs', 'xattrs')

    # Fully deprecated options
    def report_deprecation(val, old, new=None):
        if not val:
            return
        deprecation_warnings.append(
            f'{old} is deprecated and may be removed in a future version. Use {new} instead' if new
            else f'{old} is deprecated and may not work as expected')

    report_deprecation(opts.sponskrub, '--sponskrub', '--sponsorblock-mark or --sponsorblock-remove')
    report_deprecation(not opts.prefer_ffmpeg, '--prefer-avconv', 'ffmpeg')
    # report_deprecation(opts.include_ads, '--include-ads')  # We may re-implement this in future
    # report_deprecation(opts.call_home, '--call-home')  # We may re-implement this in future
    # report_deprecation(opts.writeannotations, '--write-annotations')  # It's just that no website has it

    # Dependent options
    opts.date = DateRange.day(opts.date) if opts.date else DateRange(opts.dateafter, opts.datebefore)

    if opts.exec_before_dl_cmd:
        opts.exec_cmd['before_dl'] = opts.exec_before_dl_cmd

    if opts.useid:  # --id is not deprecated in youtube-dl
        opts.outtmpl['default'] = '%(id)s.%(ext)s'

    if opts.overwrites:  # --force-overwrites implies --no-continue
        opts.continue_dl = False

    if (opts.addmetadata or opts.sponsorblock_mark) and opts.addchapters is None:
        # Add chapters when adding metadata or marking sponsors
        opts.addchapters = True

    if opts.extractaudio and not opts.keepvideo and opts.format is None:
        # Do not unnecessarily download audio
        opts.format = 'bestaudio/best'

    if opts.getcomments and opts.writeinfojson is None:
        # If JSON is not printed anywhere, but comments are requested, save it to file
        if not opts.dumpjson or opts.print_json or opts.dump_single_json:
            opts.writeinfojson = True

    if opts.allsubtitles and not (opts.embedsubtitles or opts.writeautomaticsub):
        # --all-sub automatically sets --write-sub if --write-auto-sub is not given
        opts.writesubtitles = True

    if opts.addmetadata and opts.embed_infojson is None:
        # If embedding metadata and infojson is present, embed it
        opts.embed_infojson = 'if_exists'

    # Ask for passwords
    if opts.username is not None and opts.password is None:
        opts.password = compat_getpass('Type account password and press [Return]: ')
    if opts.ap_username is not None and opts.ap_password is None:
        opts.ap_password = compat_getpass('Type TV provider account password and press [Return]: ')

    return warnings, deprecation_warnings


def get_postprocessors(opts):
    yield from opts.add_postprocessors

    if opts.parse_metadata:
        yield {
            'key': 'MetadataParser',
            'actions': opts.parse_metadata,
            'when': 'pre_process'
        }
    sponsorblock_query = opts.sponsorblock_mark | opts.sponsorblock_remove
    if sponsorblock_query:
        yield {
            'key': 'SponsorBlock',
            'categories': sponsorblock_query,
            'api': opts.sponsorblock_api,
            'when': 'after_filter'
        }
    if opts.convertsubtitles:
        yield {
            'key': 'FFmpegSubtitlesConvertor',
            'format': opts.convertsubtitles,
            'when': 'before_dl'
        }
    if opts.convertthumbnails:
        yield {
            'key': 'FFmpegThumbnailsConvertor',
            'format': opts.convertthumbnails,
            'when': 'before_dl'
        }
    if opts.extractaudio:
        yield {
            'key': 'FFmpegExtractAudio',
            'preferredcodec': opts.audioformat,
            'preferredquality': opts.audioquality,
            'nopostoverwrites': opts.nopostoverwrites,
        }
    if opts.remuxvideo:
        yield {
            'key': 'FFmpegVideoRemuxer',
            'preferedformat': opts.remuxvideo,
        }
    if opts.recodevideo:
        yield {
            'key': 'FFmpegVideoConvertor',
            'preferedformat': opts.recodevideo,
        }
    # If ModifyChapters is going to remove chapters, subtitles must already be in the container.
    if opts.embedsubtitles:
        keep_subs = 'no-keep-subs' not in opts.compat_opts
        yield {
            'key': 'FFmpegEmbedSubtitle',
            # already_have_subtitle = True prevents the file from being deleted after embedding
            'already_have_subtitle': opts.writesubtitles and keep_subs
        }
        if not opts.writeautomaticsub and keep_subs:
            opts.writesubtitles = True

    # ModifyChapters must run before FFmpegMetadataPP
    if opts.remove_chapters or sponsorblock_query:
        yield {
            'key': 'ModifyChapters',
            'remove_chapters_patterns': opts.remove_chapters,
            'remove_sponsor_segments': opts.sponsorblock_remove,
            'remove_ranges': opts.remove_ranges,
            'sponsorblock_chapter_title': opts.sponsorblock_chapter_title,
            'force_keyframes': opts.force_keyframes_at_cuts
        }
    # FFmpegMetadataPP should be run after FFmpegVideoConvertorPP and
    # FFmpegExtractAudioPP as containers before conversion may not support
    # metadata (3gp, webm, etc.)
    # By default ffmpeg preserves metadata applicable for both
    # source and target containers. From this point the container won't change,
    # so metadata can be added here.
    if opts.addmetadata or opts.addchapters or opts.embed_infojson:
        yield {
            'key': 'FFmpegMetadata',
            'add_chapters': opts.addchapters,
            'add_metadata': opts.addmetadata,
            'add_infojson': opts.embed_infojson,
        }
    # Deprecated
    # This should be above EmbedThumbnail since sponskrub removes the thumbnail attachment
    # but must be below EmbedSubtitle and FFmpegMetadata
    # See https://github.com/yt-dlp/yt-dlp/issues/204 , https://github.com/faissaloo/SponSkrub/issues/29
    # If opts.sponskrub is None, sponskrub is used, but it silently fails if the executable can't be found
    if opts.sponskrub is not False:
        yield {
            'key': 'SponSkrub',
            'path': opts.sponskrub_path,
            'args': opts.sponskrub_args,
            'cut': opts.sponskrub_cut,
            'force': opts.sponskrub_force,
            'ignoreerror': opts.sponskrub is None,
            '_from_cli': True,
        }
    if opts.embedthumbnail:
        yield {
            'key': 'EmbedThumbnail',
            # already_have_thumbnail = True prevents the file from being deleted after embedding
            'already_have_thumbnail': opts.writethumbnail
        }
        if not opts.writethumbnail:
            opts.writethumbnail = True
            opts.outtmpl['pl_thumbnail'] = ''
    if opts.split_chapters:
        yield {
            'key': 'FFmpegSplitChapters',
            'force_keyframes': opts.force_keyframes_at_cuts,
        }
    # XAttrMetadataPP should be run after post-processors that may change file contents
    if opts.xattrs:
        yield {'key': 'XAttrMetadata'}
    if opts.concat_playlist != 'never':
        yield {
            'key': 'FFmpegConcat',
            'only_multi_video': opts.concat_playlist != 'always',
            'when': 'playlist',
        }
    # Exec must be the last PP of each category
    for when, exec_cmd in opts.exec_cmd.items():
        yield {
            'key': 'Exec',
            'exec_cmd': exec_cmd,
            'when': when,
        }


def parse_options(argv=None):
    """ @returns (parser, opts, urls, ydl_opts) """
    parser, opts, urls = parseOpts(argv)
    urls = get_urls(urls, opts.batchfile, opts.verbose)

    set_compat_opts(opts)
    try:
        warnings, deprecation_warnings = validate_options(opts)
    except ValueError as err:
        parser.error(f'{err}\n')

    postprocessors = list(get_postprocessors(opts))

    any_getting = (any(opts.forceprint.values()) or opts.dumpjson or opts.dump_single_json
                   or opts.geturl or opts.gettitle or opts.getid or opts.getthumbnail
                   or opts.getdescription or opts.getfilename or opts.getformat or opts.getduration)

    any_printing = opts.print_json

    final_ext = (
        opts.recodevideo if opts.recodevideo in FFmpegVideoConvertorPP.SUPPORTED_EXTS
        else opts.remuxvideo if opts.remuxvideo in FFmpegVideoRemuxerPP.SUPPORTED_EXTS
        else opts.audioformat if (opts.extractaudio and opts.audioformat != 'best')
        else None)

    return parser, opts, urls, {
        'usenetrc': opts.usenetrc,
        'netrc_location': opts.netrc_location,
        'username': opts.username,
        'password': opts.password,
        'twofactor': opts.twofactor,
        'videopassword': opts.videopassword,
        'ap_mso': opts.ap_mso,
        'ap_username': opts.ap_username,
        'ap_password': opts.ap_password,
        'quiet': (opts.quiet or any_getting or any_printing),
        'no_warnings': opts.no_warnings,
        'forceurl': opts.geturl,
        'forcetitle': opts.gettitle,
        'forceid': opts.getid,
        'forcethumbnail': opts.getthumbnail,
        'forcedescription': opts.getdescription,
        'forceduration': opts.getduration,
        'forcefilename': opts.getfilename,
        'forceformat': opts.getformat,
        'forceprint': opts.forceprint,
        'print_to_file': opts.print_to_file,
        'forcejson': opts.dumpjson or opts.print_json,
        'dump_single_json': opts.dump_single_json,
        'force_write_download_archive': opts.force_write_download_archive,
        'simulate': (any_getting or None) if opts.simulate is None else opts.simulate,
        'skip_download': opts.skip_download,
        'format': opts.format,
        'allow_unplayable_formats': opts.allow_unplayable_formats,
        'ignore_no_formats_error': opts.ignore_no_formats_error,
        'format_sort': opts.format_sort,
        'format_sort_force': opts.format_sort_force,
        'allow_multiple_video_streams': opts.allow_multiple_video_streams,
        'allow_multiple_audio_streams': opts.allow_multiple_audio_streams,
        'check_formats': opts.check_formats,
        'listformats': opts.listformats,
        'listformats_table': opts.listformats_table,
        'outtmpl': opts.outtmpl,
        'outtmpl_na_placeholder': opts.outtmpl_na_placeholder,
        'paths': opts.paths,
        'autonumber_size': opts.autonumber_size,
        'autonumber_start': opts.autonumber_start,
        'restrictfilenames': opts.restrictfilenames,
        'windowsfilenames': opts.windowsfilenames,
        'ignoreerrors': opts.ignoreerrors,
        'force_generic_extractor': opts.force_generic_extractor,
        'ratelimit': opts.ratelimit,
        'throttledratelimit': opts.throttledratelimit,
        'overwrites': opts.overwrites,
        'retries': opts.retries,
        'file_access_retries': opts.file_access_retries,
        'fragment_retries': opts.fragment_retries,
        'extractor_retries': opts.extractor_retries,
        'skip_unavailable_fragments': opts.skip_unavailable_fragments,
        'keep_fragments': opts.keep_fragments,
        'concurrent_fragment_downloads': opts.concurrent_fragment_downloads,
        'buffersize': opts.buffersize,
        'noresizebuffer': opts.noresizebuffer,
        'http_chunk_size': opts.http_chunk_size,
        'continuedl': opts.continue_dl,
        'noprogress': opts.quiet if opts.noprogress is None else opts.noprogress,
        'progress_with_newline': opts.progress_with_newline,
        'progress_template': opts.progress_template,
        'playliststart': opts.playliststart,
        'playlistend': opts.playlistend,
        'playlistreverse': opts.playlist_reverse,
        'playlistrandom': opts.playlist_random,
        'noplaylist': opts.noplaylist,
        'logtostderr': opts.outtmpl.get('default') == '-',
        'consoletitle': opts.consoletitle,
        'nopart': opts.nopart,
        'updatetime': opts.updatetime,
        'writedescription': opts.writedescription,
        'writeannotations': opts.writeannotations,
        'writeinfojson': opts.writeinfojson,
        'allow_playlist_files': opts.allow_playlist_files,
        'clean_infojson': opts.clean_infojson,
        'getcomments': opts.getcomments,
        'writethumbnail': opts.writethumbnail is True,
        'write_all_thumbnails': opts.writethumbnail == 'all',
        'writelink': opts.writelink,
        'writeurllink': opts.writeurllink,
        'writewebloclink': opts.writewebloclink,
        'writedesktoplink': opts.writedesktoplink,
        'writesubtitles': opts.writesubtitles,
        'writeautomaticsub': opts.writeautomaticsub,
        'allsubtitles': opts.allsubtitles,
        'listsubtitles': opts.listsubtitles,
        'subtitlesformat': opts.subtitlesformat,
        'subtitleslangs': opts.subtitleslangs,
        'matchtitle': decodeOption(opts.matchtitle),
        'rejecttitle': decodeOption(opts.rejecttitle),
        'max_downloads': opts.max_downloads,
        'prefer_free_formats': opts.prefer_free_formats,
        'trim_file_name': opts.trim_file_name,
        'verbose': opts.verbose,
        'dump_intermediate_pages': opts.dump_intermediate_pages,
        'write_pages': opts.write_pages,
        'test': opts.test,
        'keepvideo': opts.keepvideo,
        'min_filesize': opts.min_filesize,
        'max_filesize': opts.max_filesize,
        'min_views': opts.min_views,
        'max_views': opts.max_views,
        'daterange': opts.date,
        'cachedir': opts.cachedir,
        'youtube_print_sig_code': opts.youtube_print_sig_code,
        'age_limit': opts.age_limit,
        'download_archive': opts.download_archive,
        'break_on_existing': opts.break_on_existing,
        'break_on_reject': opts.break_on_reject,
        'break_per_url': opts.break_per_url,
        'skip_playlist_after_errors': opts.skip_playlist_after_errors,
        'cookiefile': opts.cookiefile,
        'cookiesfrombrowser': opts.cookiesfrombrowser,
        'legacyserverconnect': opts.legacy_server_connect,
        'nocheckcertificate': opts.no_check_certificate,
        'prefer_insecure': opts.prefer_insecure,
        'http_headers': opts.headers,
        'proxy': opts.proxy,
        'socket_timeout': opts.socket_timeout,
        'bidi_workaround': opts.bidi_workaround,
        'debug_printtraffic': opts.debug_printtraffic,
        'prefer_ffmpeg': opts.prefer_ffmpeg,
        'include_ads': opts.include_ads,
        'default_search': opts.default_search,
        'dynamic_mpd': opts.dynamic_mpd,
        'extractor_args': opts.extractor_args,
        'youtube_include_dash_manifest': opts.youtube_include_dash_manifest,
        'youtube_include_hls_manifest': opts.youtube_include_hls_manifest,
        'encoding': opts.encoding,
        'extract_flat': opts.extract_flat,
        'live_from_start': opts.live_from_start,
        'wait_for_video': opts.wait_for_video,
        'mark_watched': opts.mark_watched,
        'merge_output_format': opts.merge_output_format,
        'final_ext': final_ext,
        'postprocessors': postprocessors,
        'fixup': opts.fixup,
        'source_address': opts.source_address,
        'call_home': opts.call_home,
        'sleep_interval_requests': opts.sleep_interval_requests,
        'sleep_interval': opts.sleep_interval,
        'max_sleep_interval': opts.max_sleep_interval,
        'sleep_interval_subtitles': opts.sleep_interval_subtitles,
        'external_downloader': opts.external_downloader,
        'list_thumbnails': opts.list_thumbnails,
        'playlist_items': opts.playlist_items,
        'xattr_set_filesize': opts.xattr_set_filesize,
        'match_filter': opts.match_filter,
        'no_color': opts.no_color,
        'ffmpeg_location': opts.ffmpeg_location,
        'hls_prefer_native': opts.hls_prefer_native,
        'hls_use_mpegts': opts.hls_use_mpegts,
        'hls_split_discontinuity': opts.hls_split_discontinuity,
        'external_downloader_args': opts.external_downloader_args,
        'postprocessor_args': opts.postprocessor_args,
        'cn_verification_proxy': opts.cn_verification_proxy,
        'geo_verification_proxy': opts.geo_verification_proxy,
        'geo_bypass': opts.geo_bypass,
        'geo_bypass_country': opts.geo_bypass_country,
        'geo_bypass_ip_block': opts.geo_bypass_ip_block,
        '_warnings': warnings,
        '_deprecation_warnings': deprecation_warnings,
        'compat_opts': opts.compat_opts,
    }


def _real_main(argv=None):
    setproctitle('yt-dlp')

    parser, opts, all_urls, ydl_opts = parse_options(argv)

    # Dump user agent
    if opts.dump_user_agent:
        ua = traverse_obj(opts.headers, 'User-Agent', casesense=False, default=std_headers['User-Agent'])
        write_string(f'{ua}\n', out=sys.stdout)
        sys.exit(0)

    if print_extractor_information(opts, all_urls):
        sys.exit(0)

    with YoutubeDL(ydl_opts) as ydl:
        actual_use = all_urls or opts.load_info_filename

        # Remove cache dir
        if opts.rm_cachedir:
            ydl.cache.remove()

        # Update version
        if opts.update_self:
            # If updater returns True, exit. Required for windows
            if run_update(ydl):
                if actual_use:
                    sys.exit('ERROR: The program must exit for the update to complete')
                sys.exit()

        # Maybe do nothing
        if not actual_use:
            if opts.update_self or opts.rm_cachedir:
                sys.exit()

            ydl.warn_if_short_id(sys.argv[1:] if argv is None else argv)
            parser.error(
                'You must provide at least one URL.\n'
                'Type yt-dlp --help to see a list of all options.')

        try:
            if opts.load_info_filename is not None:
                retcode = ydl.download_with_info_file(expand_path(opts.load_info_filename))
            else:
                retcode = ydl.download(all_urls)
        except DownloadCancelled:
            ydl.to_screen('Aborting remaining downloads')
            retcode = 101

    sys.exit(retcode)


def main(argv=None):
    try:
        _real_main(argv)
    except DownloadError:
        sys.exit(1)
    except SameFileError as e:
        sys.exit(f'ERROR: {e}')
    except KeyboardInterrupt:
        sys.exit('\nERROR: Interrupted by user')
    except BrokenPipeError as e:
        # https://docs.python.org/3/library/signal.html#note-on-sigpipe
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(f'\nERROR: {e}')


__all__ = [
    'main',
    'YoutubeDL',
    'parse_options',
    'gen_extractors',
    'list_extractors',
]
