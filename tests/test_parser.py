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


def test_specification_and_error_code_tables_are_reconstructed_as_rows():
    parsed=parse_pdf(V1)
    specs=by_number(parsed,'2.1')['tables']
    errors=by_number(parsed,'4.2')['tables']
    assert specs and specs[0]['rows'][0] == ['Parameter','Value']
    assert specs[0]['rows'][1] == ['Measurement method','Oscillometric']
    assert errors and errors[0]['rows'][0] == ['Code','Meaning','Device Behavior']
    assert any(row[0]=='E3' and '2 seconds' in row[2] for row in errors[0]['rows'])

def test_page_break_continuations_detected_and_flagged():
    parsed_v1 = parse_pdf(V1)
    sec31 = by_number(parsed_v1, '3.1')
    assert sec31['page_start'] == 2
    assert sec31['page_end'] == 3
    assert any('Page-break continuation' in w for w in sec31['warnings'])

    parsed_v2 = parse_pdf(V2)
    sec43 = by_number(parsed_v2, '4.3')
    assert sec43['page_start'] == 4
    assert sec43['page_end'] == 5
    assert any('Page-break continuation' in w for w in sec43['warnings'])

def test_multiline_title_merged_into_single_document_title():
    parsed = parse_pdf(V1)
    title_node = next(n for n in parsed['nodes'] if n['node_type'] == 'document_title')
    assert title_node['level'] == 0
    assert 'CardioTrack CT-200 Home Blood Pressure Monitor' in title_node['heading']
    assert 'Technical & User Manual' in title_node['heading']

def test_numbered_lists_preserved_as_body_not_nodes():
    parsed = parse_pdf(V1)
    sec33 = by_number(parsed, '3.3')
    assert '1. Normal:' in sec33['body_text']
    assert '5. Hypertensive Crisis:' in sec33['body_text']
    # Verify the numbered items under 3.3 did not get falsely created as section nodes
    top_level = [n['section_number'] for n in parsed['nodes'] if n['level'] == 1]
    assert top_level == ['1', '2', '3', '4', '5', '6', '7', '8']

