"""Deliver one-source dialogue plans as editable Premiere XML and approved MP4.

python helpers/dialogue_delivery.py plan.json -o edit/delivery-v5 --xml-only
python helpers/dialogue_delivery.py plan.json -o edit/delivery-v5 --xml-only --qa qa.json
python helpers/dialogue_delivery.py plan.json -o edit/delivery-v5 --render --approved
python helpers/dialogue_delivery.py --finalize edit/delivery-v5 --qa qa.json

Output directories must be new. Plans contain source, ranges [{start,end}], and
optionally removals [{id,start,end,reason}], notes [{id,start,end,text}]. Times
are source seconds. Supplied output_start/output_end and source metadata are
checked, never trusted. Only CFR, progressive SDR, square-pixel video with one
mono/stereo audio stream is supported; unsupported media fails explicitly.
COMPLETE describes technical export. Editorial READY requires current evidence
validated with --qa; otherwise the bundle is REVIEW_REQUIRED. Only READY bundles
update latest pointers. --finalize rechecks an existing export after later QA.
Reviewer declarations are validated; this tool does not itself listen to speech.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from fractions import Fraction
from pathlib import Path, PureWindowsPath
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse


def run(args):
    p = subprocess.run([str(x) for x in args], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[-6000:])
    return p.stdout


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def media_uri(path):
    """Premiere's Windows FCP7 importer requires localhost and escaped drive colon."""
    w = PureWindowsPath(str(path))
    if w.drive:
        if w.drive.startswith('\\\\'):
            raise ValueError('UNC sources unsupported: use a local drive path')
        return 'file://localhost/' + quote(w.as_posix(), safe='/')
    return Path(path).resolve().as_uri()


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path]))


def media_info(path):
    if not Path(path).is_file():
        raise ValueError(f'Source does not exist: {path}')
    p = probe(path)
    vs = [s for s in p['streams'] if s['codec_type'] == 'video']
    au = [s for s in p['streams'] if s['codec_type'] == 'audio']
    if len(vs) != 1 or len(au) != 1 or au[0]['channels'] not in (1, 2):
        raise ValueError('Requires exactly one video and one mono/stereo audio stream')
    v, a = vs[0], au[0]
    fps = Fraction(v['avg_frame_rate'])
    allowed = {Fraction(n) for n in (24, 25, 30, 48, 50, 60)} | {Fraction(n, 1001) for n in (24000, 30000, 60000)}
    if fps not in allowed or fps != Fraction(v['r_frame_rate']):
        raise ValueError('Unsupported or variable frame rate; normalize source first')
    if v.get('field_order', 'progressive') not in ('progressive', 'unknown') or v.get('sample_aspect_ratio', '1:1') not in ('1:1', 'N/A'):
        raise ValueError('Interlaced or non-square-pixel source unsupported')
    if v.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
        raise ValueError('HDR source requires explicit tone mapping before this SDR delivery')
    if any(float(s.get('start_time', 0)) != 0 for s in (v, a)):
        raise ValueError('Nonzero stream starts require normalization to preserve sync')
    # Inspect every presentation timestamp, not just nominal/average rate (VFR can match both).
    timestamps = run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'csv=p=0', path])
    pts = [float(line.split(',')[0]) for line in timestamps.splitlines() if line.split(',')[0].strip()]
    if not pts or abs(pts[0]) > 1e-4 or any(abs((b-a)-float(1/fps)) > 1e-4 for a,b in zip(pts, pts[1:])):
        raise ValueError('Video timestamps are not continuous CFR starting at zero')
    tc=next((s.get('tags',{}).get('timecode') for s in p['streams'] if s.get('tags',{}).get('timecode')),p.get('format',{}).get('tags',{}).get('timecode','00:00:00:00'))
    tc_frame,tc_format=timecode_frames(tc,fps)
    return dict(fps=str(fps), frames=len(pts), width=v['width'], height=v['height'], channels=a['channels'], samplerate=int(a['sample_rate']),timecode=tc,timecode_frame=tc_frame,timecode_format=tc_format)


def timecode_frames(tc,fps):
    match=re.fullmatch(r'(\d{2}):(\d{2}):(\d{2})([:;])(\d{2})',tc)
    if not match:raise ValueError(f'Unsupported source timecode: {tc}')
    h,m,s,sep,f=match.groups();h,m,s,f=map(int,(h,m,s,f));base=round(Fraction(fps))
    if h>=24 or m>=60 or s>=60 or f>=base:raise ValueError('Invalid source timecode')
    count=((h*60+m)*60+s)*base+f
    if sep==';':
        if Fraction(fps) not in (Fraction(30000,1001),Fraction(60000,1001)):raise ValueError('Unsupported drop-frame timecode rate')
        drop=2 if base==30 else 4
        if m%10 and s==0 and f<drop:raise ValueError('Invalid dropped timecode frame label')
        minutes=h*60+m;count-=drop*(minutes-minutes//10)
    return count,'DF' if sep==';' else 'NDF'


def frame(value, fps):
    exact = Fraction(str(value)) * Fraction(fps)
    n = round(exact)
    if abs(float(exact - n)) > 0.001:
        raise ValueError(f'Not frame-aligned: {value} seconds at {fps} fps')
    return n


def canonical_plan(plan, info):
    fps, full = info['fps'], info['frames']
    if 'source_fps' in plan and abs(float(Fraction(str(plan['source_fps']))) - float(Fraction(fps))) > 1e-6:
        raise ValueError('Stale source_fps')
    if 'source_duration' in plan and frame(plan['source_duration'], fps) != full:
        raise ValueError('Stale source_duration')
    ranges, cursor, previous = [], 0, 0
    for row in plan['ranges']:
        a, b = frame(row['start'], fps), frame(row['end'], fps)
        if not 0 <= previous <= a < b <= full:
            raise ValueError('Ranges must be positive, ordered, nonoverlapping and inside source')
        end = cursor + b-a
        for key, expected in [('output_start', cursor), ('output_end', end)]:
            if key in row and frame(row[key], fps) != expected:
                raise ValueError(f'Stale {key}')
        ranges.append([a,b,cursor,end]); cursor, previous = end,b
    if not ranges:
        raise ValueError('Empty edit')
    if 'total_duration_s' in plan and frame(plan['total_duration_s'], fps) != cursor:
        raise ValueError('Stale total_duration_s')
    removals = []
    for row in plan.get('removals', []):
        a,b = frame(row['start'], fps), frame(row['end'], fps)
        if not 0 <= a < b <= full or any(max(a,x) < min(b,y) for x,y,_,_ in ranges):
            raise ValueError('Removal is invalid or overlaps retained frames')
        removals.append(dict(row, start_frame=a, end_frame=b))
    if 'removals' in plan:
        intervals = sorted([(a,b) for a,b,_,_ in ranges] + [(r['start_frame'], r['end_frame']) for r in removals])
        edge = 0
        for a,b in intervals:
            if a > edge:
                raise ValueError(f'Unaccounted source frames {edge}..{a}')
            edge = max(edge,b)
        if edge != full:
            raise ValueError('Unaccounted source tail')
    notes = []
    for row in plan.get('notes', [])+plan.get('technical_reviews', []):
        # Review notes may be approximate seconds; markers never define cut points.
        a = round(Fraction(str(row['start'])) * Fraction(fps))
        b = round(Fraction(str(row.get('end', row['start']))) * Fraction(fps))
        if not 0 <= a <= b <= full:
            raise ValueError('Note outside source')
        notes.append(dict(row, start_frame=a, end_frame=b))
    marker_rows = removals + notes
    requested = plan.get('timeline_marker_ids')
    if requested is not None:
        if (not isinstance(requested, list) or len(requested) != len(set(requested)) or
                any(not isinstance(x, str) or not x for x in requested)):
            raise ValueError('timeline_marker_ids must be a unique list of nonempty ids')
        indexed = {row.get('id'): row for row in marker_rows}
        if any(ident not in indexed for ident in requested):
            raise ValueError('timeline_marker_ids refers to an unknown review item')
        marker_rows = [indexed[ident] for ident in requested]
    audio_mode = plan.get('xml_audio_mode', 'source')
    if audio_mode not in ('source', 'mono-left'):
        raise ValueError('xml_audio_mode must be source or mono-left')
    return dict(ranges=ranges, removals=removals, notes=notes, markers=marker_rows,
                xml_audio_mode=audio_mode, output_frames=cursor)


def map_source(t, ranges):
    return sum(max(0, min(t,b)-a) for a,b,_,_ in ranges)


def build_xml(source, info, edit):
    def e(parent, tag, value=None, **attrs):
        n = ET.SubElement(parent, tag, attrs)
        if value is not None: n.text = str(value)
        return n
    fps = Fraction(info['fps'])
    def rate(parent):
        r=e(parent,'rate'); e(r,'timebase',round(fps)); e(r,'ntsc','TRUE' if fps.denominator==1001 else 'FALSE')
    def sample(parent):
        s=e(parent,'samplecharacteristics'); rate(s)
        for k,v in [('width',info['width']),('height',info['height']),('anamorphic','FALSE'),('pixelaspectratio','Square'),('fielddominance','none')]: e(s,k,v)
    root=ET.Element('xmeml',version='5'); seq=e(root,'sequence',id='dialogue-edited')
    e(seq,'name','Dialogue edited');e(seq,'duration',edit['output_frames']);rate(seq)
    tc=e(seq,'timecode');rate(tc);e(tc,'string','00:00:00:00');e(tc,'frame',0);e(tc,'displayformat','NDF')
    m=e(seq,'media');v=e(m,'video');sample(e(v,'format')); tracks=[e(v,'track')]
    output_channels = 1 if edit.get('xml_audio_mode') == 'mono-left' else info['channels']
    audio=e(m,'audio');e(audio,'numOutputChannels',output_channels)
    outputs=e(audio,'outputs')
    for ch in range(1,output_channels+1):
        group=e(outputs,'group');e(group,'index',ch);e(group,'numchannels',1);e(group,'downmix',0)
        channel=e(group,'channel');e(channel,'index',ch)
        tr=e(audio,'track');e(tr,'outputchannelindex',ch);tracks.append(tr)
    wrote_file=False
    for i,(a,b,c,d) in enumerate(edit['ranges'],1):
        for ch,tr in enumerate(tracks):
            clip=e(tr,'clipitem',id=f'clip-{i}-{ch}')
            e(clip,'name',f'{i:03d} | {Path(source).name}');e(clip,'enabled','TRUE');e(clip,'duration',info['frames']);rate(clip)
            for k,val in [('start',c),('end',d),('in',a),('out',b)]:e(clip,k,val)
            f=e(clip,'file',id='original-source')
            if not wrote_file:
                e(f,'name',Path(source).name);e(f,'pathurl',media_uri(source));rate(f);e(f,'duration',info['frames'])
                ft=e(f,'timecode');rate(ft);e(ft,'string',info.get('timecode','00:00:00:00'));e(ft,'frame',info.get('timecode_frame',0));e(ft,'displayformat',info.get('timecode_format','NDF'))
                fm=e(f,'media');fv=e(fm,'video');e(fv,'duration',info['frames']);sample(fv)
                fa=e(fm,'audio');fs=e(fa,'samplecharacteristics');e(fs,'depth',16);e(fs,'samplerate',info['samplerate']);e(fa,'channelcount',info['channels'])
                wrote_file=True
            st=e(clip,'sourcetrack');e(st,'mediatype','audio' if ch else 'video');e(st,'trackindex',ch or 1)
            for linked in range(len(tracks)):
                link=e(clip,'link');e(link,'linkclipref',f'clip-{i}-{linked}');e(link,'mediatype','audio' if linked else 'video');e(link,'trackindex',linked or 1);e(link,'clipindex',i)
                if linked:e(link,'groupindex',1)
    for row in edit.get('markers',edit['removals']+edit['notes']):
        marker=e(seq,'marker');e(marker,'name',row.get('id','REVIEW'));e(marker,'comment',row.get('reason',row.get('text','Review'))+' | '+row.get('keep_note','')+f" | source {row['start']}..{row.get('end',row['start'])} s")
        start=min(map_source(row['start_frame'],edit['ranges']),edit['output_frames']-1)
        e(marker,'in',start);e(marker,'out',-1)
    ET.indent(root)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def validate_render(path, info, expected_frames, decode=True):
    p=probe(path);v=next(s for s in p['streams'] if s['codec_type']=='video');a=next(s for s in p['streams'] if s['codec_type']=='audio')
    expected=expected_frames/float(Fraction(info['fps']))
    if int(v.get('nb_frames',-1))!=expected_frames or Fraction(v['avg_frame_rate'])!=Fraction(info['fps']):
        raise ValueError('Rendered video frame count/rate mismatch')
    if (v['width'],v['height'],a['channels']) != (info['width'],info['height'],info['channels']):
        raise ValueError('Rendered dimensions/channels mismatch')
    for stream in (v,a):
        if abs(float(stream.get('start_time',0)))>.025 or abs(float(stream['duration'])-expected)>.05:
            raise ValueError('Rendered AV timing mismatch')
    if decode:run(['ffmpeg','-v','error','-xerror','-i',path,'-f','null','-'])
    return dict(frames=expected_frames,duration_s=expected,full_decode_passed=decode)


def validate_xml(path, source, info, edit):
    root=ET.parse(path).getroot()
    seqs=root.findall('sequence')
    if len(seqs)!=1 or int(seqs[0].findtext('duration'))!=edit['output_frames']:
        raise ValueError('XML sequence duration/count mismatch')
    seq=seqs[0];tracks=seq.findall('./media/video/track')+seq.findall('./media/audio/track')
    output_channels = 1 if edit.get('xml_audio_mode') == 'mono-left' else info['channels']
    if len(tracks)!=1+output_channels:raise ValueError('XML track count mismatch')
    if int(seq.findtext('./media/audio/numOutputChannels')) != output_channels:
        raise ValueError('XML output channel count mismatch')
    ids={c.attrib['id'] for tr in tracks for c in tr.findall('clipitem')}
    for ch,tr in enumerate(tracks):
        clips=tr.findall('clipitem')
        if len(clips)!=len(edit['ranges']):raise ValueError('XML clip count mismatch')
        for i,(cl,(a,b,c,d)) in enumerate(zip(clips,edit['ranges']),1):
            for tag,expected in [('in',a),('out',b),('start',c),('end',d),('duration',info['frames'])]:
                if int(cl.findtext(tag))!=expected:raise ValueError(f'XML {tag} mismatch')
            refs=[x.text for x in cl.findall('./link/linkclipref')]
            if set(refs)!={f'clip-{i}-{channel}' for channel in range(len(tracks))} or any(ref not in ids for ref in refs):
                raise ValueError('XML AV linking mismatch')
    urls=root.findall('.//pathurl')
    if len(urls)!=1 or urls[0].text!=media_uri(source):raise ValueError('XML source URI mismatch')
    if not Path(source).is_file():raise ValueError('XML source no longer exists')
    parsed=urlparse(urls[0].text);decoded=unquote(parsed.path)
    if PureWindowsPath(str(source)).drive:
        if parsed.netloc!='localhost' or PureWindowsPath(decoded.lstrip('/'))!=PureWindowsPath(str(source)):
            raise ValueError('XML Windows URI does not round trip to source')
    elif Path(decoded).resolve()!=Path(source).resolve():raise ValueError('XML URI does not round trip to source')
    definition=root.find('.//file[pathurl]')
    if int(definition.findtext('duration'))!=info['frames'] or int(definition.findtext('./timecode/frame'))!=info.get('timecode_frame',0):
        raise ValueError('XML full source duration/timecode mismatch')
    markers=seq.findall('marker');marker_rows=edit.get('markers',edit['removals']+edit['notes'])
    if len(markers)!=len(marker_rows):raise ValueError('XML marker count mismatch')
    for marker,row in zip(markers,marker_rows):
        if int(marker.findtext('in'))!=min(map_source(row['start_frame'],edit['ranges']),edit['output_frames']-1):
            raise ValueError('XML marker position mismatch')
    return True


def render(source, info, edit, source_hash, out, cache):
    cache.mkdir(parents=True,exist_ok=True);paths=[];fps=float(Fraction(info['fps']))
    settings=dict(codec='libx264',crf=20,preset='fast',pixel_format='yuv420p',audio='pcm_s16le',fade_s=.03,info=info)
    for i,(a,b,_,_) in enumerate(edit['ranges']):
        key=json_hash(dict(source=source_hash,start=a,end=b,settings=settings));dest=cache/f'{key}.mov';stamp=cache/f'{key}.json'
        cached=dest.exists() and stamp.exists() and json.loads(stamp.read_text()).get('sha256')==digest(dest)
        if not cached:
            duration=(b-a)/fps;fade=min(.03,duration/2)
            run(['ffmpeg','-y','-v','error','-ss',str(a/fps),'-i',source,'-t',str(duration),'-map','0:v:0','-map','0:a:0','-af',f'afade=t=in:d={fade},afade=t=out:st={duration-fade}:d={fade}', '-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-r',info['fps'],'-c:a','pcm_s16le','-ar',str(info['samplerate']),dest])
            validate_render(dest,info,b-a)
            stamp.write_text(json.dumps(dict(sha256=digest(dest))))
        paths.append(dest)
        print(f'Segment {i+1}/{len(edit["ranges"])} verified',flush=True)
    listing=cache/f'concat-{json_hash(paths.__str__())}.txt'
    listing.write_text(''.join("file '"+p.resolve().as_posix().replace("'","'\\''")+"'\n" for p in paths),encoding='utf-8')
    run(['ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',listing,'-map','0:v:0','-map','0:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-movflags','+faststart',out])
    return validate_render(out,info,edit['output_frames'])


def qa_module():
    spec=importlib.util.spec_from_file_location('dialogue_qa',Path(__file__).with_name('dialogue_qa.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def publish_ready(folder, manifest):
    """Draft exports must never replace a reviewed latest pointer."""
    if manifest.get('editorial_status')!='READY':return
    pointer=folder.parent/('latest-render.json' if manifest['mode']=='render' else 'latest-xml.json')
    temp=pointer.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(directory=str(folder.resolve()),manifest_sha256=digest(folder/'manifest.json'))),encoding='utf-8')
    temp.replace(pointer)


def invalidate_pointer(folder):
    for name in ('latest-render.json','latest-xml.json'):
        pointer=folder.parent/name
        if not pointer.exists():continue
        try:
            target=json.loads(pointer.read_text(encoding='utf-8')).get('directory')
            if target and Path(target).resolve()==folder.resolve():pointer.unlink()
        except (ValueError, OSError):
            # Never delete an unrelated or unparseable pointer.
            pass


def finalize(folder, report):
    """Revalidate an existing immutable bundle before accepting later QA."""
    folder=Path(folder).resolve();manifest_path=folder/'manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    try:
        if manifest.get('mode') not in ('render','xml-only'):
            raise ValueError('Unsupported bundle mode')
        plan_path=folder/'plan.json';plan_bytes=plan_path.read_bytes()
        plan_file_hash=hashlib.sha256(plan_bytes).hexdigest();plan=json.loads(plan_bytes.decode('utf-8-sig'))
        if json_hash(plan)!=manifest.get('plan_sha256'):
            raise ValueError('Bundle plan hash mismatch')
        if 'plan_file_sha256' in manifest and plan_file_hash!=manifest['plan_file_sha256']:
            raise ValueError('Bundle plan file hash mismatch')
        # Raw plan bytes may declare a source relative to the original plan folder.
        # The export manifest preserves its already resolved original source.
        source=Path(manifest['source']).resolve()
        if digest(source)!=manifest.get('source_sha256'):
            raise ValueError('Bundle source hash mismatch')
        info=media_info(source);edit=canonical_plan(plan,info)
        if info!=manifest.get('media'):
            raise ValueError('Bundle media metadata mismatch')
        for field in ('source_sha256','source_hash'):
            if field in plan and plan[field]!=manifest['source_sha256']:
                raise ValueError('Stale plan source fingerprint')
        xml_name=manifest.get('xml',{}).get('file')
        if not isinstance(xml_name,str) or Path(xml_name).name!=xml_name:
            raise ValueError('Bundle XML filename invalid')
        xml=folder/xml_name
        if digest(xml)!=manifest['xml']['sha256']:
            raise ValueError('Bundle XML hash mismatch')
        validate_xml(xml,source,info,edit)
        mp4=None
        if manifest['mode']=='render':
            mp4_name=manifest.get('mp4',{}).get('file')
            if not isinstance(mp4_name,str) or Path(mp4_name).name!=mp4_name:
                raise ValueError('Bundle MP4 filename invalid')
            mp4=folder/mp4_name
            if digest(mp4)!=manifest['mp4']['sha256']:
                raise ValueError('Bundle MP4 hash mismatch')
            manifest['render_validation']=validate_render(mp4,info,edit['output_frames'])
        result=qa_module().validate_qa(report,plan_path,source,info,edit,mp4)
        # Catch concurrent edits between validation and publication.
        if digest(source)!=manifest['source_sha256'] or digest(xml)!=manifest['xml']['sha256'] or digest(plan_path)!=plan_file_hash or (mp4 is not None and digest(mp4)!=manifest['mp4']['sha256']):
            raise ValueError('Bundle changed during finalization')
        qa_module().recheck_bindings(result['validated_files'])
        manifest.update(status='COMPLETE',export_status='COMPLETE',editorial_status=result['editorial_status'],
                        qa=result,plan_file_sha256=plan_file_hash,mp4_ready=mp4 is not None)
        manifest.pop('qa_error',None);manifest.pop('error',None)
        manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        if result['editorial_status']!='READY':invalidate_pointer(folder)
        publish_ready(folder,manifest)
        return manifest
    except Exception as exc:
        manifest.update(editorial_status='REVIEW_REQUIRED',qa_error=str(exc))
        manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        invalidate_pointer(folder)
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan',type=Path,nargs='?');parser.add_argument('-o','--output',type=Path)
    parser.add_argument('--finalize',type=Path);parser.add_argument('--qa',type=Path)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--xml-only',action='store_true');mode.add_argument('--render',action='store_true')
    parser.add_argument('--approved',action='store_true');parser.add_argument('--cache',type=Path)
    args=parser.parse_args(argv)
    if args.finalize:
        if not args.qa or args.plan or args.output or args.xml_only or args.render or args.approved or args.cache:
            parser.error('--finalize DIRECTORY requires --qa REPORT and no export arguments')
        print(json.dumps(finalize(args.finalize,args.qa),indent=2));return
    if not args.plan or not args.output or not (args.xml_only or args.render):
        parser.error('Export requires plan, --output, and --xml-only or --render')
    if args.render and not args.approved:parser.error('--render requires --approved after user approval of this plan')
    if args.output.exists():raise ValueError('Output directory already exists; choose a new version directory')
    plan_bytes=args.plan.read_bytes();plan_file_hash=hashlib.sha256(plan_bytes).hexdigest()
    plan=json.loads(plan_bytes.decode('utf-8-sig'));source=Path(plan['source'])
    if not source.is_absolute():source=args.plan.parent/source
    source=source.resolve();source_hash=digest(source)
    info=media_info(source);edit=canonical_plan(plan,info);plan_hash=json_hash(plan)
    if digest(source)!=source_hash or digest(args.plan)!=plan_file_hash:
        raise ValueError('Source or plan changed during media validation')
    for field in ('source_sha256','source_hash'):
        if field in plan and plan[field]!=source_hash:raise ValueError('Stale source fingerprint')
    # Fail stale supplied XML QA before creating a draft. Render QA must also
    # bind the actual new MP4, so it is checked after the render exists.
    if args.qa and args.xml_only:qa_module().validate_qa(args.qa,args.plan,source,info,edit)
    args.output.mkdir(parents=True)
    manifest=dict(status='IN_PROGRESS',export_status='IN_PROGRESS',editorial_status='REVIEW_REQUIRED',mode='render' if args.render else 'xml-only',source=str(source),source_sha256=source_hash,plan_sha256=plan_hash,plan_file_sha256=plan_file_hash,media=info,mp4_ready=False,premiere_import_tested=False,editorial_qa='Reviewer evidence required; delivery does not certify listening')
    def save(): (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    save()
    try:
        (args.output/'plan.json').write_bytes(plan_bytes)
        xml=args.output/'Dialogue_Premiere.xml';xml.write_bytes(build_xml(source,info,edit));validate_xml(xml,source,info,edit)
        manifest['xml']=dict(file=xml.name,sha256=digest(xml),structural_validation=True)
        mp4=None
        if args.render:
            mp4=args.output/'Dialogue.mp4'
            manifest['render_validation']=render(source,info,edit,source_hash,mp4,args.cache or args.output.parent/'dialogue-cache')
            manifest['mp4']=dict(file=mp4.name,sha256=digest(mp4));manifest['mp4_ready']=True
        if digest(source)!=source_hash or digest(args.plan)!=plan_file_hash or digest(args.output/'plan.json')!=plan_file_hash:
            raise ValueError('Source or plan changed during delivery')
        manifest.update(status='COMPLETE',export_status='COMPLETE')
        if args.qa:
            result=qa_module().validate_qa(args.qa,args.output/'plan.json',source,info,edit,mp4)
            manifest.update(editorial_status=result['editorial_status'],qa=result)
            qa_module().recheck_bindings(result['validated_files'])
        if digest(source)!=source_hash or digest(args.plan)!=plan_file_hash or digest(args.output/'plan.json')!=plan_file_hash or digest(xml)!=manifest['xml']['sha256'] or (mp4 is not None and digest(mp4)!=manifest['mp4']['sha256']):
            raise ValueError('Source, plan or export changed during QA')
        save();publish_ready(args.output,manifest)
        print(json.dumps(manifest,indent=2))
    except Exception as exc:
        manifest.update(status='FAILED' if manifest['export_status']!='COMPLETE' else 'COMPLETE',
                        export_status='FAILED' if manifest['export_status']!='COMPLETE' else 'COMPLETE',
                        editorial_status='REVIEW_REQUIRED',error=str(exc));save();raise


if __name__=='__main__':main()
