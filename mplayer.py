#!/usr/bin/env python2
# -*- coding: utf-8 -*-
#
# Copyright 2010-2013 Bing Sun <subi.the.dream.walker@gmail.com>
# Time-stamp: <2013-01-05 10:55:55 by subi>
#
# mplayer-wrapper is an MPlayer frontend, trying to be a transparent interface.
# It is convenient to rename the script to "mplayer" and place it in your $PATH
# (don't overwrite the real MPlayer); you would not even notice its existence.

# TODO:
# * data persistance for
#    i)   resume last played position
#    ii)  remember last settings (volume/hue/contrast etc.)
#    iii) dedicated dir for subtitles
#    iv)  MPlayerContext
#    v)   sub_delay info from shooter
# * remember last volume/hue/contrast for continuous playing (don't need data
#   persistance)
# * shooter sometimes return a false subtitle with the same time length. find a
#   cure. (using zenity, pygtk, or both?)
# * xset s off
# * "not compiled in option"
# * detect the language in embedded subtitles, which is guaranteed to be utf8
# * use ffprobe for better(?) metainfo detection?

from __future__ import unicode_literals

import logging
import os,sys
import subprocess, threading, time
import hashlib
import re
from fractions import Fraction
from collections import defaultdict

### Helper classes and functions
# http://www.python.org/dev/peps/pep-0318/
def singleton(cls):
    instances = {}
    def getinstance():
        if cls not in instances:
            instances[cls] = cls()
        return instances[cls]
    return getinstance

def which(prog):
    paths = [''] if os.path.isabs(prog) else os.environ['PATH'].split(os.pathsep)
    for path in paths:
        fullpath = os.path.join(path, prog)
        if os.access(fullpath, os.X_OK):
            return fullpath
    return None

def check_screen_dim():
    '''Select the maximal available screen dimension.
    '''
    dim = Dimension()
    dim_updated = False
    
    if which('xrandr'):
        for l in subprocess.check_output(['xrandr']).splitlines():
            if l.startswith('*'): # xrandr 1.1
                dim_updated = True
                _,w,_,h = l.split()
                if w > dim.width:
                    dim = Dimension(w,h)
            elif '*' in l:        # xrandr 1.2 and above
                dim_updated = True
                w,h = l.split()[0].split('x')
                if w > dim.width:
                    dim = Dimension(w,h)

    if not dim_updated:
        logging.info('Cannot find xrandr or unsupported xrandr version. The screen dimension defaults to {0}x{1}.'.format(dim.width, dim.height))
        
    return dim

class Dimension(object):
    def __init__(self, width = 640, height = 480):
        self.width = int(width)
        self.height = int(height)
        self.aspect = Fraction(self.width,self.height) if not self.height == 0 else Fraction(0)

def guess_locale_and_convert(txt, precise=False):
    ascii = '[\x09\x0A\x0D\x20-\x7E]'

    # http://en.wikipedia.org/wiki/GBK
    gbk = ['[\xA1-\xA9][\xA1-\xFE]',              # Level GBK/1
           '[\xB0-\xF7][\xA1-\xFE]',              # Level GBK/2
           '[\x81-\xA0][\x40-\x7E\x80-\xFE]',     # Level GBK/3
           '[\xAA-\xFE][\x40-\x7E\x80-\xA0]',     # Level GBK/4
           '[\xA8-\xA9][\x40-\x7E\x80-\xA0]',     # Level GBK/5
           '[\xAA-\xAF][\xA1-\xFE]',              # user-defined
           '[\xF8-\xFE][\xA1-\xFE]',              # user-defined
           '[\xA1-\xA7][\x40-\x7E\x80-\xA0]']     # user-defined
    gb2312 = gbk[0:2]

    # http://en.wikipedia.org/wiki/Big5
    # http://www.khngai.com/chinese/charmap/tblbig.php?page=0
    big5 = ['[\x81-\A0][\x40-\x7E\xA1-\xFE]',     # Reserved for user-defined characters
            '[\xA1-\xA2][\x40-\x7E\xA1-\xFE]',    # "Graphical characters"
            '\xA3[\x40-\x7E\xA1-\xBF]',
            '\xA3[\xC0-\xFE]',                    # Reserved, not for user-defined characters
            '[\xA4-\xC5][\x40-\x7E\xA1-\xFE]',    # Frequently used characters
            '\xC6[\x40-\x7E]',
            '\xC6[\xA1-\xFE]',                    # Reserved for user-defined characters
            '[\xC7\xC8][\x40-\x7E\xA1-\xFE]',
            '[\xC9-\xF8][\x40-\x7E\xA1-\xFE]',    # Less frequently used characters
            '\xF9[\x40-\x7E\xA1-\xD5]',
            '\xF9[\xD6-\xFE]',                    # Reserved for user-defined characters
            '[\xFA-\xFE][\x40-\x7E\xA1-\xFE]']    

    # http://www.w3.org/International/questions/qa-forms-utf-8
    utf_8 = ['[\xC2-\xDF][\x80-\xBF]',            # non-overlong 2-byte
             '\xE0[\xA0-\xBF][\x80-\xBF]',        # excluding overlongs
             '[\xE1-\xEC\xEE\xEF][\x80-\xBF]{2}', # straight 3-byte
             '\xED[\x80-\x9F][\x80-\xBF]',        # excluding surrogates
             '\xF0[\x90-\xBF][\x80-\xBF]{2}',     # planes 1-3
             '[\xF1-\xF3][\x80-\xBF]{3}',         # planes 4-15
             '\xF4[\x80-\x8F][\x80-\xBF]{2}']     # plane 16

    def count_in_codec(txt, pattern):
        '''ASCII bytes (\x00-\x7F) can be standalone or be the low
        byte in the pattern. We count them separately.

        Param:
            pattern: the list of code points
        Return: (#ASCII, #PATTERN, #OTHER)
        '''
        pattern = '|'.join(pattern)
            
        # collecting all patterns
        pat = '({0}|{1})'.format(ascii, pattern)
        interpretable = ''.join(re.findall(pat, txt))

        # collecting standalone ASCII by filtering 'pattern' out
        pat = '(?:{0})+'.format(pattern)
        standalone_ascii = ''.join(re.split(pat, interpretable))

        return len(standalone_ascii), len(interpretable)-len(standalone_ascii), len(s)-len(interpretable)

    # the defaults
    enc,lang = 'ascii','und'
    
    # BOM are trivial to detect
    # http://unicode.org/faq/utf_bom.html#BOM
    bom_list = [('\x00\x00\xFE\xFF', 'utf_32_be'),
                ('\xFF\xFE\x00\x00', 'utf_32_le'),
                ('\xFE\xFF',         'utf_16_be'),
                ('\xFF\xFE',         'utf_16_le'),
                ('\xEF\xBB\xBF',     'utf_8'),
                ]
    for bom in bom_list:
        if txt.startswith(bom[0]):
            enc = bom[1]
            txt = txt[len(bom[0]):]
            if not enc == 'utf_8':
                txt = txt.decode(enc,'ignore').encode('utf_8')
            return enc,lang,txt

    # filter out ASCII as much as possible by the heuristic that a \x00-\x7F
    # byte that doesn't follow a \x80-\xFF byte is a REAL ascii byte.
    sample = ''.join(re.split('(?:(?<![\x80-\xFE]){0})+'.format(ascii), txt))

    # take first 2k bytes
    if len(sample)>2048:
        sample = sample[0:2048]
    # if we have more than 0.5% (~10) characters cannot be interpreted by the
    # codec pattern, reject it
    threshold = int(len(sample) * .005)

    # To guess the encoding of a byte string, we count the bytes those cannot
    # be interpreted by the codec.
    if count_in_codec(sample, utf_8)[2] < threshold:
        enc = 'utf_8'
    elif precise:
        # GB2312 and BIG5 share lots of code points and hence sometimes we need
        # a heuristic method to try to distinguish them.
        
        # http://www.ibiblio.org/pub/packages/ccic/software/data/chrecog.gb.html
        l = len(re.findall('[\xA1-\xFE][\x40-\x7E]',sample))
        h = len(re.findall('[\xA1-\xFE][\xA1-\xFE]',sample))
        if l == 0:
            enc,lang = 'gb2312','chs'
        elif float(l)/float(h) < 0.25:
            enc,lang = 'gbk','chi'
        else:
            enc,lang = 'big5','cht'
    elif count_in_codec(sample, gbk)[2] < threshold:
        # Favor GBK over BIG5. If this not you want, set precise=True
        enc,lang = 'gbk','chi'
    elif count_in_codec(samples, big5)[2] < threshold:
        enc,lang = 'big5','cht'

    if not enc in ['utf_8', 'ascii']:
        txt = txt.decode(enc,'ignore').encode('utf_8')

    return enc,lang,txt
        
### Application classes
class Application(object):
    '''The application class will:
    1. parse command line arguments
    2. provide run() method to accomplish its role
    '''
    def __init__(self, args):
        logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)

        if '--debug' in args:
            logging.root.setLevel(logging.DEBUG)
            args.remove('--debug')
            config['debug'] = True
        if '--dry-run' in args:
            logging.root.setLevel(logging.DEBUG)
            args.remove('--dry-run')
            config['dry-run'] = True
            config['debug'] = True

class Identifier(Application):
    def run(self):
        print MPlayer().identify(self.args)
        
    def __init__(self,args):
        super(Identifier, self).__init__(args)
        self.args = args

class Player(Application):
    def __init__(self, args):
        super(Player, self).__init__(args)
        self.args = defaultdict(list)
        
        while args:
            s = args.pop(0)
            if s == '--':
                self.args['file'] += args
                args = []
            elif s.startswith('-'):
                flag = MPlayer().has_opt(s.partition('-')[2])
                if flag == 0:
                    self.args['invalid'].append(s)
                elif flag == 1:
                    self.args['valid'].append(s)
                elif flag == 2:
                    self.args['valid'].append(s)
                    if args:
                        self.args['valid'].append(args.pop(0))
            else:
                self.args['file'].append(s)
            
        if self.args['invalid']:
            logging.info('Unsupported options "' + ' '.join(self.args['invalid']) + '" are automatically suppressed.')

    def run(self):
        if not self.args['file']:
            MPlayer().play(self.args['valid'])
        else:
            files = self.args['file'] + find_more_episodes(self.args['file'][-1])

            for f in files:
                m = Media(f, self.args['valid'])
                m.prepare_mplayer_args()
                watch_thread = threading.Thread(target=self.watch, args=(m,))
                watch_thread.daemon = True
                watch_thread.start()

                MPlayer().play(m)
                if MPlayer().last_exit_status == 'Quit':
                    break

    def watch(self, media):
        if media.is_video():
            media.fetch_remote_subtitles_and_save(load_in_mplayer=True)
            
class Fetcher(Application):
    def run(self):
        for f in self.files:
            Media(f).fetch_remote_subtitles_and_save(sub_savedir=self.savedir)
            
    def __init__(self, args):
        self.savedir = None
        self.files = []
        
        super(Fetcher,self).__init__(args)
        for arg in args:
            if arg.startswith('--savedir'):
                self.savedir = arg.split('=')[1]
            else:
                self.files += [arg]

### Main modules
class MPlayerFifo(object):
    '''MPlayerFifo will maintain a FIFO for IPC with mplayer.
    '''
    def send(self, s):
        if self.args:
            logging.debug('Sending message "{0}" to {1}...'.format(s, self.__path))
            with open(self.__path,'w') as f:
                f.write(s+'\n')
        else:
            logging.info('"{0}" cannot be sent to the non-existing {1}.'.format(s, self.__path))
    
    def __init__(self):
        # don't use __del__() to release resource because MPlayerFifo tends to
        # be used in a daemon thread and hence may result in circular reference.
        import atexit

        self.args = []
        xdg = os.environ['XDG_RUNTIME_DIR']
        if xdg:
            self.__path = os.path.join(xdg, 'mplayer.fifo')
        else:
            import tempfile
            tmpdir = tempfile.mkdtemp()
            atexit.register(lambda d: os.rmdir(d), tmpdir)
            self.__path = os.path.join(tmpdir, 'mplayer.fifo')

        try:
            os.mkfifo(self.__path)
            atexit.register(lambda f: os.unlink(f), self.__path)
            self.args = '-input file={0}'.format(self.__path)
        except OSError as e:
            logging.info(e)
            
def refine_video_geometry(width,height,DAR_advice,DAR_force=None):
    ''' Guess the best video geometry.
    
    References:
    1. http://www.mir.com/DMG/aspect.html
    2. http://en.wikipedia.org/wiki/Pixel_aspect_ratio
    3. http://lipas.uwasa.fi/~f76998/video/conversion
    '''
    # storage aspect ratio
    storage = Dimension(width,height)

    # DAR(display aspect ratio): assume aspects of 4:3, 16:9, or >16:9
    # (wide-screen movies)
    def aspect_not_stupid(ar):
        return abs(ar-Fraction(4,3))<0.02 or ar-Fraction(16,9)>-0.02

    # FIXME: luca waltz_bus stop.mp4
    if DAR_force:
        DAR = DAR_force
    elif aspect_not_stupid(DAR_advice):
        DAR = Fraction(DAR_advice).limit_denominator(9)
    else:
        # let's guess
        if storage.width >= 1280:
            # http://en.wikipedia.org/wiki/High-definition_television
            # HDTV is always 16:9
            DAR = Fraction(16,9)
        elif storage.width >= 700:
            # http://en.wikipedia.org/wiki/Enhanced-definition_television
            # EDTV can be 4:3 or 16:9, blame the video ripper if we
            # took the wrong guess
            DAR = Fraction(16,9)
        else:
            # http://en.wikipedia.org/wiki/Standard-definition_television
            # SDTV can be 4:3 or 16:9, blame the video ripper if we
            # took the wrong guess
            DAR = Fraction(4,3)
    # pixel/sample aspect ratio
    PAR = (DAR/storage.aspect).limit_denominator(82)
    return storage, DAR, PAR
    
class Media(object):
    def is_video(self):
        return self.__info['video']
    
    def mplayer_args(self):
        return self.args

    def prepare_mplayer_args(self):
        # always do unrar in case we have rar-ed vobsub files.
        unrar = which('unrar')
        if unrar:
            self.add_arg('-unrarexec {0}'.format(unrar))
            
        # collect media info by midentify
        self.__raw_info['mplayer'] = defaultdict(list)
        raw = self.__raw_info['mplayer']

        for l in MPlayer().identify(self.args).splitlines():
            k,_,v = l.partition('=')
            raw[k].append(v)
            
        info = self.__info
        if raw['ID_VIDEO_ID']:
            info['video'] = True

            if '-noass' in self.args or not MPlayer().support_ass():
                self.add_arg('-noass')
            else:
                self.add_arg('-ass')

            if '-aspect' in self.args:
                s = self.args[self.args.index('-aspect')+1]
                if ':' in s:
                    x,y = s.split(':')
                    DAR_force = Fraction(int(x),int(y))
                else:
                    DAR_force = Fraction(s)
            elif 'dsize' in self.args:
                # TODO
                pass
            else:
                DAR_force = None
            w,h = raw['ID_VIDEO_WIDTH'][0], raw['ID_VIDEO_HEIGHT'][0]
            a = float(raw['ID_VIDEO_ASPECT'][0]) if raw['ID_VIDEO_ASPECT'] else 0.0
            info['storage'], info['DAR'], info['PAR'] = refine_video_geometry(w,h,a,DAR_force)
            
            # non-square pixel fix
            if info['PAR'] != 1:
                # work-around for stretched osd/subtitle after applying -aspect
                sw = info['storage'].width
                sh = info['storage'].height
                if info['storage'].width >= 1280:
                    sh = int(sw / info['DAR'])
                else:
                    sw = int(sh * info['DAR'])
                self.add_arg('-aspect {0.numerator}:{0.denominator}'.format(info['DAR']))
                self.add_arg('-vf-pre scale={0}:{1}'.format(sw,sh))

            # video expanding
            for item in expand_video(info['DAR'], check_screen_dim().aspect):
                self.add_arg(item)

            # subtitles
            self.parse_local_subtitles()

    def parse_local_subtitles(self):
        info = self.__info
        raw = self.__raw_info['mplayer']
        
        info['subtitle'] = defaultdict(bool)
        if raw['ID_SUBTITLE_ID']:
            # TODO: extract subtitles and combine to a bi-lingual sub
            # ffmpeg -i Seinfeld.2x01.The_Ex-Girlfriend.xvid-TLF.mkv -vn -an -scodec srt sub.srt
            info['subtitle']['embed'] = []
            for i in raw['ID_SUBTITLE_ID']:
                info['subtitle']['embed'] += raw['ID_SID_{0}_LANG'.format(i)]
        if raw['ID_FILE_SUB_ID']:
            info['subtitle']['external'] = raw['ID_FILE_SUB_FILENAME']
            logging.debug('Converting the external subtitles to UTF-8...')
            for subfile in raw['ID_FILE_SUB_FILENAME']:
                # open in binary mode because we don't know the encoding
                with open(subfile,'r+b') as f:
                    s = f.read()
                    enc,_,s = guess_locale_and_convert(s)
                    if not enc in ['utf_8','ascii']:
                        f.seek(0)
                        f.write(s)
            self.add_arg('-subcp utf8')
        if raw['ID_VOBSUB_ID']:
            info['subtitle']['vobsub'] = True
        
    def fetch_remote_subtitles_and_save(self, sub_savedir=None, load_in_mplayer=False):
        info = self.__info

        if not info['subtitle']:
            # if parse_local_subtitles() not done
            info['subtitle'] = defaultdict(bool)

        if info['subtitle']['embed'] and set(info['subtitle']['embed'])&{'chs','cht','chn','chi','zh','tw','hk'}:
            # have Chinese text subtitles
            pass
        elif info['subtitle']['external']:
            # TODO: language?
            pass
        else:
            # whatever
            info['subtitle']['remote'] = fetch_shooter(info['abspath'],info['shash'])

        if info['subtitle']['remote']:
            force_utf8_and_filter_duplicates(info['subtitle']['remote'])
            save_to_disk(info['subtitle']['remote'], info['abspath'], sub_savedir)

            if load_in_mplayer:
                for s in info['subtitle']['remote']:
                    MPlayer().send('sub_load "{0}"'.format(s['path']))
                MPlayer().send('sub_file 0')
            
    def add_arg(self,arg,force=False):
        never_overwritten = ['-vf-pre','-vf-add']
        arg = arg.split()
        if force or arg[0] in never_overwritten or not arg[0] in self.args:
            self.args += arg

    def __init__(self,path,global_args=[]):
        self.args = global_args[:] + [path]
        
        self.__info = defaultdict(bool)
        self.__raw_info = defaultdict(bool)
        self.__info['path'] = path

        if not os.path.exists(path):
            return

        # basic info
        info = self.__info
        info['abspath'] = os.path.abspath(info['path'])
        sz = os.path.getsize(info['path'])
        if sz>8192:
            with open(info['path'],'rb') as f:
                info['shash'] = ';'.join([(f.seek(s), hashlib.md5(f.read(4096)).hexdigest())[1] for s in (lambda l:[4096, l/3*2, l/3, l-8192])(sz)])

    def __del__(self):
        if not config['debug']:
            return
        info = self.__info
        log_items = ['  Fullpath:  {0}'.format(info['abspath']),
                     '  Hash:      {0}'.format(info['shash'])]
        if info['video']:
            log_items.append('  Dimension: {0} [PAR {1} DAR {2}]'
                             .format('{0.width}x{0.height}'.format(info['storage']),
                                     '{0.numerator}:{0.denominator}'.format(info['PAR']),
                                     '{0.numerator}:{0.denominator}'.format(info['DAR'])))
        logging.debug('\n'+'\n'.join(log_items))
        
@singleton
class MPlayer(object):
    last_timestamp = 0.0
    last_exit_status = None

    def send(self, cmd):
        if self.__process != None:
            self.__fifo.send(cmd)
        
    def play(self, media):
        if isinstance(media,Media):
            media.add_arg(self.__fifo.args)
            args = [self.exe_path] + media.mplayer_args()
        else:
            args = [self.exe_path] + media
        logging.debug('\n'+' '.join(args))
        if not config['dry-run']:
            self.__process = subprocess.Popen(args, stdin=sys.stdin, stdout=subprocess.PIPE, stderr=None)
            self.__tee()

    def identify(self, args):
        args = [self.exe_path] + '-vo null -ao null -frames 0 -identify'.split() + args
        if config['debug']:
            logging.debug('Executing:\n  {0}'.format(' '.join(args)))
        return '\n'.join([l for l in subprocess.check_output(args).splitlines() if l.startswith('ID_')])

    def support_ass(self):
        if self.__ass == None:
            self.__ass = True
            if not self.has_opt('ass'):
                self.__ass = False
            else:
                libass_path = None
                for l in subprocess.check_output(['ldd',self.exe_path]).splitlines():
                    if 'libass' in l:
                        libass_path = l.split()[2]
                if not libass_path:
                    self.__ass = False
                else:
                    if not 'libfontconfig' in subprocess.check_output(['ldd',libass_path]):
                        self.__ass = False
        return self.__ass
    
    def is_mplayer2(self):
        if self.__mplayer2 == None:
            if 'MPlayer2' in subprocess.check_output([self.exe_path]).splitlines()[0]:
                logging.debug('Is a MPlayer2 fork.')
                self.__mplayer2 = True
            else:
                self.__mplayer2 = False
        return self.__mplayer2
    
    def has_opt(self, opt):
        '''return value:
        0: don't have the option
        1: have it and take no param
        2: have it and take 1 param
        '''
        if not self.__opts:
            self.__gen_opts()
        return self.__opts[opt] if opt in self.__opts else 0

    def __init__(self):
        self.exe_path = None
        for p in ['/opt/bin/mplayer','/usr/local/bin/mplayer','/usr/bin/mplayer']:
            if which(p):
                self.exe_path = p
                break

        self.__fifo = MPlayerFifo()
        self.__mplayer2 = None
        self.__ass = None
        self.__opts = {}

    def __gen_opts(self):
        options = subprocess.Popen([self.exe_path, '-list-options'], stdout=subprocess.PIPE).communicate()[0].splitlines()
        if self.is_mplayer2():
            options = options[3:len(options)-3]
        else:
            options = options[3:len(options)-4]

        for line in options:
            s = line.split()
            opt = s[0].split(':') # take care of option:suboption
            if opt[0] in self.__opts:
                continue
            self.__opts[opt[0]] = (2 if len(opt)==2 or s[1]!='Flag' else 1)

        # handle vf* af*:
        # mplayer reports option name as vf*, which is a family of options.
        del self.__opts['af*']
        del self.__opts['vf*']
        for extra in ['af','af-adv','af-add','af-pre','af-del','vf','vf-add','vf-pre','vf-del']:
            self.__opts[extra] = 2
        for extra in ['af-clr','vf-clr']:
            self.__opts[extra] = 1

    def __tee(self):
        def flush_first_line(fileobj, lines):
            fileobj.write(''.join(lines.pop(0)))
            fileobj.flush()
            lines.append([])

        f = sys.stdout
        p = self.__process
        
        # cache 5 lines in case of unexpected outputs
        lines = [[] for i in range(5)]
        while True:
            c = p.stdout.read(1)
            if not c:
                break
            lines[4].append(c)

            # carriage return / linefeed
            if c == '\n':
                flush_first_line(f,lines)
            elif c == '\r':
                d = p.stdout.read(1)
                if d == '\n':
                    lines[4].append('\n')
                    flush_first_line(f,lines)
                else:
                    flush_first_line(f,lines)
                    lines[4].append(d)
            else:
                pass

        # save info and flush rest outputs
        for l in (''.join(ll) for ll in lines):
            if l.startswith(('A:','V:')):
                try:
                    self.last_timestamp = float(l[2:9])
                except ValueError:
                    pass
            if l.startswith('Exiting...'):
                self.last_exit_status = l[12:len(l)-2]
            f.write(l)
        f.flush()

        logging.debug('Last timestamp: {0}'.format(self.last_timestamp))
        logging.debug('Last exit status: {0}'.format(self.last_exit_status))
        self.__process = None

def save_to_disk(subtitles, filepath, save_dir=None):
    prefix,_ = os.path.splitext(filepath)
    if save_dir:
        prefix = os.path.join(save_dir, os.path.basename(prefix))

    # save subtitles
    for s in subtitles:
        suffix = '.' + s['lang'] if not s['lang'] == 'und' else ''
                
        path = prefix + suffix + '.' + s['extension']
        if os.path.exists(path):
            path = prefix + suffix + '1.' + s['extension']
        with open(path,'wb') as f:
            f.write(s['content'])
            logging.info('Saved the subtitle as {0}'.format(path))
            s['path'] = path

def force_utf8_and_filter_duplicates(subtitles):
    logging.debug('Trying to filter duplicated subtitles...')

    for s in subtitles:
        _,s['lang'],s['content'] = guess_locale_and_convert(s['content'])
            
    dup_tag = [False]*len(subtitles)
    for i in range(len(subtitles)):
        if dup_tag[i]:
            continue
        for j in range(i+1, len(subtitles)):
            sa = subtitles[i]
            sb = subtitles[j]
            if sa['extension'] != sb['extension'] or sa['lang'] != sb['lang']:
                continue
            import difflib
            similarity = difflib.SequenceMatcher(None, sa['content'], sb['content']).real_quick_ratio()
            logging.debug('Similarity is {0}.'.format(similarity))
            if similarity > 0.9:
                dup_tag[j] = True
    # TODO: reserve longer subtitles 
    subtitles = [subtitles[i] for i in range(len(subtitles)) if not dup_tag[i]]
    logging.debug('{0} subtitle(s) reserved after duplicates filtering.'.format(len(subtitles)))

def parse_shooter_package(fileobj):
    '''Parse shooter returned package of subtitles.
    Return subtitles encoded in UTF-8.
    '''
    subtitles = []
    f = fileobj

    # read contents
    import struct
    c = f.read(1)
    package_count = struct.unpack('!b', c)[0]

    for i in range(package_count):
        # NOTE: '_' is the length of following byte-stream
        c = f.read(8)
        _,desc_length = struct.unpack('!II', c)
        description = f.read(desc_length).decode('utf_8')
        sub_delay = description.partition('=')[2] / 1000.0 if description and 'delay' in description else 0
        if description:
            logging.debug('Subtitle description: {0}'.format(description))

        c = f.read(5)
        _,file_count = struct.unpack('!IB', c)
            
        for j in range(file_count):
            c = f.read(8)
            _,ext_len = struct.unpack('!II', c)
            ext = f.read(ext_len)

            c = f.read(4)
            file_len = struct.unpack('!I', c)[0]
            sub = f.read(file_len)
            if sub.startswith('\x1f\x8b'):
                import gzip
                from cStringIO import StringIO
                sub = gzip.GzipFile(fileobj=StringIO(sub)).read()

            subtitles.append({'extension': ext,
                              'delay': sub_delay,
                              'content': sub})

    logging.debug('{0} subtitle(s) fetched.'.format(len(subtitles)))
    return subtitles

def fetch_shooter(filepath,filehash):
    import httplib
    schemas = ['http', 'https'] if hasattr(httplib, 'HTTPS') else ['http']
    servers = ['www', 'splayer', 'svplayer'] + ['splayer'+str(i) for i in range(1,13)]
    splayer_rev = 2437 # as of 2012-07-02
    tries = [2, 10, 30, 60, 120]

    # generate data for submission
    head,tail = os.path.split(filepath)
    pathinfo= '\\'.join(['D:', os.path.basename(head), tail])
    vstring = 'SP,aerSP,aer {0} &e(\xd7\x02 {1} {2}'.format(splayer_rev, pathinfo, filehash)
    vhash = hashlib.md5(vstring).hexdigest()
    import random
    boundary = '-'*28 + '{0:x}'.format(random.getrandbits(48))

    header = [('User-Agent', 'SPlayer Build {0}'.format(splayer_rev)),
              ('Content-Type', 'multipart/form-data; boundary={0}'.format(boundary))
              ]
    items = [('filehash', filehash), ('pathinfo', pathinfo), ('vhash', vhash)]
    data = ''.join(['--{0}\n'
                    'Content-Disposition: form-data; name="{1}"\n\n'
                    '{2}\n'.format(boundary, *d) for d in items]
                   + ['--' + boundary + '--'])

    if config['dry-run']:
        print 'DRY-RUN: Were trying to fetch subtitles for {0}.'.format(filepath)
        return None
        
#        app.send('osd_show_text "正在查询字幕..." 5000')
#            app.send('osd_show_text "查询字幕失败." 3000')

    # fetch
    import urllib2
    for i, t in enumerate(tries):
        try:
            logging.debug('Wait for {0}s to reconnect (Try {1} of {2})...'.format(t,i+1,len(tries)+1))
            time.sleep(t)

            url = '{0}://{1}.shooter.cn/api/subapi.php'.format(random.choice(schemas), random.choice(servers))
            req = urllib2.Request(url)
            for h in header:
                req.add_header(*h)
            req.add_data(data)
            logging.debug('Connecting server {0} with the submission:\n'
                          '\n{1}\n'
                          '{2}\n'.format(url,
                                         '\n'.join(['{0}:{1}'.format(*h) for h in header]),
                                         data))

            # todo: with context manager
            response = urllib2.urlopen(req)
            fetched_subtitles = parse_shooter_package(response)
            response.close()

            if fetched_subtitles:
                break
        except urllib2.URLError, e:
            logging.debug(e)
    return fetched_subtitles

def expand_video(source_aspect, target_aspect):
    '''This function does 3 things:
    1. Video Expansion:
       Attach two black bands to the top and bottom of a video so that MPlayer
       can put OSD/texts in the bands. Be ware that the video expansion will
       change the video size (normally height), e.g. a 1280x720 video will be
       1280x960 after an expansion towards 4:3.

       Doing expansion is trivial because there is a '-vf expand' MPlayer
       option. '-vf expand' can change the size of a video to fit a specific
       aspect by attaching black bands (not scaling!). The black bands may be
       attached vertically when changing towards a smaller aspect (e.g. 16:9 to
       4:3 conversion), or horizontal vise versa (e.g. 4:3 to 16:9 conversion).

       The '-ass-use-margins' was once used instead because of the
       incompatibility (subtitle overlapping issue) between '-vf expand' and
       the '-ass' subtitle renderer. This method can only attach black bands
       vertically ('-ass-top-margin' and '-ass-bottom-margin').
           
    2. Font-size Normalization:
       Make the subtitle font be the same size when displayed full-screen in
       the same screen for any video.
           
    3. Expansion Compactness:
       Place the subtitle as close to the picture as possible (instead of the
       very top/bottom of the screen, which is visual distracting).

    The problems of the Font-size Normalization and Expansion Compactness bring
    the main complexities. To understand what has been done here, we first
    describe how the font size is determined in MPlayer.

    There are two OSD/subtitle renderers in MPlayer: one depends FreeType and
    the other libass. OSD is always rendered through FreeType; text subtitles
    (not vobsub) are rendered through libass if '-ass' is specified, or
    FreeType otherwise.

    As for mplayer2, the FreeType renderer has been dropped completely since
    Aug. 2012; libass is used for all the OSD/subtitle rendering.
    (http://git.mplayer2.org/mplayer2/commit/?id=083e6e3e1a9bf9ee470f59cfd1c775c8724c5ed9)
    The -noass option is indeed applying a plain ASS style that mimics the
    FreeType renderer.

    From here on, (X,Y) denotes the display size of a video. Remember that its
    aspect ratio is subject to being changed by '-vf expand'.
        
    Here are the details:
    1. The FreeType Renderer:
                      font_size = movie_size * font_scale_factor / 100
       Simple and clear.

       The movie_size is determined by the option '-subfont-autoscale':
       a. subfont-autoscale = 0 (no autoscale):
                      movie_size = 100
       b. subfont-autoscale = 1 (proportional to video height):
                      movie_size = Y
       c. subfont-autoscale = 2 (proportional to video width):
                      movie_size = X
       d. subfont-autoscale = 3 (proportional to video diagonal):
                      movie_size = sqrt(X^2+Y^2)

       The font_scale_factor is assigned by the option '-subfont-text-scale'
       and '-subfont-osd-scale', which defaults to 3.5 and 4.0, respectively,
       and applies to subtitles and OSD, respectively.
           
    2. The libass Renderer:
       MPlayer provides some ASS styles for libass; font-scale involved styles
       are:
       i).  PlayResX,PlayResY
            They are the source frame (i.e. the reference frame for ASS style
            with an absolute value, e.g. FontSize and Margins).

            PlayResY defaults to 288 and. PlayResX defaults to PlayResY/1.3333,
            which is 384.
       ii). ScaleX,ScaleY
            These font scales default to 1.
       iii).FontSize
            As pointed above, the FontSize is the size in the reference frame
            (PlayResX,PlayResY).
                      FontSize = PlayResY * font_scale_factor / 100

            The font_scale_factor is again assigned by the option
            '-subfont-text-scale' and '-subfont-osd-scale', which applies to
            subtitles and OSD, respectively. (Be ware that OSD is rendered by
            libass in mplayer2.)
                      
            The option '-subfont-autoscale' will affect the FontSize as
            well. Although this is in fact meaningless because the font size is
            always proportional to video height only in libass renderer, a
            correction that corresponds to about 4:3 aspect ratio video is
            applied to get a size somewhat closer to what non-libass rendering.
            a. subfont-autoscale = 0 (no autoscale):
                      ignored
            b. subfont-autoscale = 1 (proportional to video height):
                      ignored
            c. subfont-autoscale = 2 (proportional to video width):
                      FontSize *= 1.3
            d. subfont-autoscale = 3 (proportional to video diagonal):
                      FontSize *= 1.4 (mplayer)
                      FontSize *= 1.7 (mplayer2)
       iv). font_size_coeff
            This is not an ASS style but an extra parameter for libass. The
            value is specified by the '-ass-font-scale' MPlayer option.

       libass does the real work as follows:
       i). determine the scale factor for (PlayResX,PlayResY)->(X,Y)
                      font_scale = font_size_coeff * Y/PlayResY
       ii).apply the font scale:
                      font_x = FontSize * font_scale * ScaleX
                      font_y = FontSize * font_scale * ScaleY

       Combining all the procedure and supposing that the ASS styles take
       default values, the font size displayed in frame (X,Y) is:
               font_size = font_size_coeff * font_scale_factor * Y/100  (*)

    Now let's try the font size normalization. What we need is the Y_screen
    proportionality of the font size. (Of course you can use X or diagonal
    instead of Y).
    1. If we don't care the expansion compactness, the video will be expanded
       to the screen aspect, hence Y = Y_screen. There is nothing to be done
       for libass renderer, as the font size is proportional to Y (see *).

       For the FreeType renderer, as long as subfont-autoscale is enabled, the
       font size is always proportional to Y because of the fixed aspect.

    2. If we want to make the expansion compact, the display aspect of the
       expanded video will be typically larger than the screen aspect. Let's do
       some calculations:
       a. display_aspect > screen_aspect
                    font_size ∝ Y = Y_screen / display_aspect:screen_aspect
       b. display_aspect = screen_aspect
                    font_size ∝ Y = Y_screen
       c. display_aspect < screen_aspect
          Won't happen because we do horizontal expansion if display_aspect <
          screen_aspect so the result is always display_aspect = screen_aspect.

    Let font_scale_factor *= display_aspect:screen_aspect resolve all the
    problem. For FreeType renderer, we will want to set subfont-autoscale=1 or
    else an additional correction has to be applied.
    '''
    args = []
    # the video is too narrow (<4:3); assume 4:3
    if source_aspect < Fraction(4,3):
        args.append('-vf-add dsize=4/3')

    # be proportional to height
    args.append('-subfont-autoscale 1')

    # default scales
    scale_base_factor = 1.5
    subfont_text_scale = 3.5 * scale_base_factor
    subfont_osd_scale = 4 * scale_base_factor

    # expansion compactness:
    # a margin take 1.8 lines of 18-pixels font in screen of height 288
    margin_scale = 18.0/288.0 * 1.8 * scale_base_factor
    expected_aspect = source_aspect / (1+2*margin_scale)
    if expected_aspect > target_aspect:
        display_aspect = expected_aspect
        subfont_text_scale *= expected_aspect/target_aspect
    else:
        display_aspect = target_aspect

    # generate MPlayer args
    args.append('-vf-add expand=::::1:{0}'.format(display_aspect))
    args.append('-subfont-text-scale {0}'.format(subfont_text_scale))
    args.append('-subfont-osd-scale {0}'.format(subfont_osd_scale))

    return args
       
def find_more_episodes(filepath):
    '''Try to find some following episodes/parts.
    '''
    def translate(s):
        import locale
        dic = dict(zip('零壹贰叁肆伍陆柒捌玖〇一二三四五六七八九','0123456789'*2))
        return ''.join([dic.get(c,c) for c in s])
    def split_by_int(s):
        return [(int(x) if x.isdigit() else x) for x in re.split('(\d+)', translate(s)) if x != '']
    def strip_to_int(s,prefix):
        # strip the prefix
        if prefix and s.startswith(prefix):
            _,_,s = s.partition(prefix)
        # extract the first int
        val = split_by_int(s)[0]
        return val if isinstance(val,int) else -1

    if not os.path.exists(filepath):
        return []

    pdir, basename = os.path.split(os.path.abspath(filepath))
    _, ext = os.path.splitext(basename)
    # basic candidate filtering
    # 1. extention
    files = [f for f in os.listdir(pdir) if f.endswith(ext)]
    # 2. remove previous episodes
    files.sort(key=split_by_int)
    del files[0:files.index(basename)]

    # not necessary to go further if no candidates
    if len(files) == 1:
        return []

    # find the common prefix
    i_break = 0
    for i in range(min(len(files[0]),len(files[1]))):
        if not files[0][i] == files[1][i]:
           i_break = i
           break
    prefix = files[0][0:i_break]

    # generate the list
    results = []
    for i,f in enumerate(files[1:]):
        if strip_to_int(f,prefix) - strip_to_int(files[i],prefix) == 1:
            results.append(os.path.join(pdir,f))
        else:
            break
    return results

if __name__ == '__main__':
    if sys.hexversion < 0x02070000:
        print 'Please run the script with python>=2.7'
    else: 
        config = defaultdict(bool)
        config['enc'] = sys.getfilesystemencoding()
        
        args = [x.decode(config['enc']) for x in sys.argv]
        name = os.path.basename(args.pop(0))
        if 'mplayer' in name:
            app = Player
        elif 'mfetch' in name:
            app = Fetcher
        elif 'midentify' in name:
            app = Identifier
        else:
            app = Application

        app(args).run()
