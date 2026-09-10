import importlib.util
from pathlib import Path
import unittest
import tempfile
import xml.etree.ElementTree as ET

spec=importlib.util.spec_from_file_location('delivery',Path(__file__).parents[1]/'helpers/dialogue_delivery.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.info=dict(fps='25',frames=100,width=640,height=360,channels=2,samplerate=48000)
        self.plan=dict(ranges=[dict(start=0,end=1),dict(start=2,end=4)],removals=[dict(id='CUT',start=1,end=2,reason='false start')])

    def test_windows_uri(self):
        self.assertEqual(d.media_uri(r'C:\Footage folder\mój #1.mp4'),'file://localhost/C%3A/Footage%20folder/m%C3%B3j%20%231.mp4')

    def test_conservation_and_marker(self):
        edit=d.canonical_plan(self.plan,self.info)
        self.assertEqual(edit['output_frames'],75)
        self.assertEqual(d.map_source(40,edit['ranges']),25)
        root=ET.fromstring(d.build_xml(r'C:\source.mp4',self.info,edit))
        self.assertEqual(root.findtext('.//marker/in'),'25')
        clips=root.findall('.//clipitem')
        self.assertEqual(len(clips),6)
        self.assertTrue(all(c.findtext('duration')=='100' for c in clips))
        self.assertEqual([c.findtext('in') for c in root.findall('.//video/track/clipitem')],['0','50'])
        ids={c.attrib['id'] for c in clips}
        self.assertTrue(all(l.text in ids for l in root.findall('.//linkclipref')))

    def test_stale_plan(self):
        for mutation in [dict(source_fps=30),dict(source_duration=5),dict(total_duration_s=4)]:
            with self.assertRaises(ValueError):d.canonical_plan(dict(self.plan,**mutation),self.info)
        self.plan['ranges'][1]['output_start']=2
        with self.assertRaisesRegex(ValueError,'Stale output_start'):d.canonical_plan(self.plan,self.info)

    def test_invalid_ranges(self):
        for ranges in [[dict(start=.01,end=1)],[dict(start=2,end=1)],[dict(start=2,end=3),dict(start=1,end=2)]]:
            with self.assertRaises(ValueError):d.canonical_plan(dict(ranges=ranges),self.info)

    def test_unaccounted_and_overlap(self):
        self.plan['removals'][0]['end']=1.8
        with self.assertRaisesRegex(ValueError,'Unaccounted'):d.canonical_plan(self.plan,self.info)
        self.plan['removals'][0]['end']=2.2
        with self.assertRaisesRegex(ValueError,'overlaps'):d.canonical_plan(self.plan,self.info)

    def test_render_needs_approval(self):
        with self.assertRaises(SystemExit):d.main(['missing.json','-o','unused-output','--render'])

    def test_missing_source(self):
        with self.assertRaisesRegex(ValueError,'does not exist'):d.media_info('nonexistent-media-945823.mp4')

    def test_timecodes(self):
        self.assertEqual(d.timecode_frames('09:43:46:22','25'),(875672,'NDF'))
        self.assertEqual(d.timecode_frames('01:00:00;00','30000/1001'),(107892,'DF'))
        with self.assertRaises(ValueError):d.timecode_frames('00:01:00;00','30000/1001')

    def test_independent_xml_validation_detects_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'source.mp4';source.write_bytes(b'fixture')
            xml=Path(td)/'edit.xml';edit=d.canonical_plan(self.plan,self.info)
            xml.write_bytes(d.build_xml(source,self.info,edit))
            self.assertTrue(d.validate_xml(xml,source,self.info,edit))
            root=ET.parse(xml);root.find('.//clipitem/in').text='1';root.write(xml)
            with self.assertRaisesRegex(ValueError,'XML in mismatch'):d.validate_xml(xml,source,self.info,edit)

    def test_technical_markers_and_keep_note(self):
        self.plan['technical_reviews']=[dict(id='T1',start=.14,end=.5,reason='Check consonant')]
        self.plan['removals'][0]['keep_note']='Keep full last take'
        edit=d.canonical_plan(self.plan,self.info)
        root=ET.fromstring(d.build_xml(r'C:\source.mp4',self.info,edit))
        self.assertEqual(len(root.findall('.//marker')),2)
        self.assertIn('Keep full last take',root.findtext('.//marker/comment'))

    def test_timeline_marker_ids_exports_only_requested_review_marker(self):
        self.plan['notes']=[dict(id='F01',start=.5,end=.8,text='Pickup: 28–200 mm')]
        self.plan['technical_reviews']=[dict(id='T1',start=.14,end=.5,reason='Internal review')]
        self.plan['timeline_marker_ids']=['F01']
        edit=d.canonical_plan(self.plan,self.info)
        root=ET.fromstring(d.build_xml(r'C:\source.mp4',self.info,edit))
        markers=root.findall('.//marker')
        self.assertEqual([marker.findtext('name') for marker in markers],['F01'])
        self.assertIn('Pickup: 28–200 mm',markers[0].findtext('comment'))

    def test_mono_left_xml_uses_one_audio_track_from_stereo_source(self):
        self.plan['xml_audio_mode']='mono-left'
        edit=d.canonical_plan(self.plan,self.info)
        root=ET.fromstring(d.build_xml(r'C:\source.mp4',self.info,edit))
        self.assertEqual(root.findtext('./sequence/media/audio/numOutputChannels'),'1')
        self.assertEqual(len(root.findall('./sequence/media/audio/track')),1)
        self.assertEqual({x.findtext('trackindex') for x in root.findall('./sequence/media/audio/track/clipitem/sourcetrack')},{'1'})
        self.assertEqual(root.findtext('.//file/media/audio/channelcount'),'2')

    def test_existing_version_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError,'already exists'):
                d.main(['missing-plan.json','-o',td,'--xml-only'])


if __name__=='__main__':unittest.main()
