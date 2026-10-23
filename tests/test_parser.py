from pathlib import Path
from app.parser import parse_pdf

ROOT=Path(__file__).parents[1]
V1=ROOT/'data'/'ct200_manual.pdf'
V2=ROOT/'data'/'ct200_manual_v2.pdf'

def by_number(parsed, number):
    return next(n for n in parsed['nodes'] if n['section_number']==number)

def test_skipped_numbering_attaches_to_nearest_existing_ancestor():
    parsed=parse_pdf(V1)
    node=by_number(parsed,'2.1.1.1')
    assert node['parent_logical_key']=='SECTION:2.1'
    assert any('skips 2.1.1' in w for w in node['warnings'])

def test_out_of_order_sections_are_preserved_and_flagged():
    parsed=parse_pdf(V1)
    numbers=[n['section_number'] for n in parsed['nodes']]
    assert numbers.index('3.4') < numbers.index('3.3')
    assert any('before section 3.3' in w for w in by_number(parsed,'3.4')['warnings'])

def test_duplicate_heading_text_remains_distinct_under_correct_parents():
    parsed=parse_pdf(V1)
    a=by_number(parsed,'4.2'); b=by_number(parsed,'7.1')
    assert a['title']=='Error Codes' and b['title']=='Error Codes'
    assert a['logical_key'] != b['logical_key']
    assert a['parent_logical_key']=='SECTION:4'
    assert b['parent_logical_key']=='SECTION:7'

def test_v2_threshold_changes_are_detectable_by_hash():
    a=parse_pdf(V1); b=parse_pdf(V2)
    old=by_number(a,'2.1.1.1'); new=by_number(b,'2.1.1.1')
    assert old['logical_key']==new['logical_key']
    assert old['body_text'] != new['body_text']

def test_v2_adds_error_code_and_data_export_sections():
    parsed=parse_pdf(V2)
    numbers={n['section_number'] for n in parsed['nodes']}
    assert '5.3' in numbers
    assert '4.2' in numbers
    assert '4.2' in numbers and '5.3' in numbers
