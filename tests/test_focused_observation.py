"""Task-relevant observations retain controls beyond long header/navigation lists."""
from web_agent.dom_inspector import compact_dom


def test_late_link_and_article_scope_survive_bounded_observation():
    html = ('<h1>Topic</h1>' + '<a href="/navigation">Menu</a>' * 100 +
            '<article id="body" class="content"><p>Complete prose with first and last facts.</p>'
            '<a class="related" href="/other" target="_blank"><span>Related subject</span></a></article>')
    view = compact_dom(html, 5, max_html=1500, focus_terms=['Related subject'])
    assert view['count'] == 5 and view['truncated']
    assert view['elements'][0]['text'] == 'Related subject'
    assert view['elements'][0]['href_path'] == '/other'
    assert '/other' in view['html']
    assert any(r['id'] == 'body' and r['class'] == 'content' for r in view['content_regions'])
    assert view['untrusted_page_data']


def test_unfocused_observation_preserves_control_order_and_redaction():
    view = compact_dom('<input id="first"><article id="body">token=synthetic'
                       '<a href="/last">Last</a></article>', 1)
    assert view['elements'][0]['id'] == 'first'
    assert 'synthetic' not in view['html']
    assert any(r['id'] == 'body' for r in view['content_regions'])


def test_section_anchor_is_preserved_and_plain_prose_does_not_become_a_link():
    view = compact_dom('<a href="#3-2">Subject / Related</a>'
                       '<article id="prose"><p><span>Related is a plain mention</span></p></article>'
                       '<a href="#token=synthetic">Private</a>', focus_terms=['Related'])
    assert view['elements'][0]['href_path'] == '#3-2'
    assert 'href="#3-2"' in view['html']
    assert 'synthetic' not in view['html']
    assert any(x['tag'] == 'span' and 'Related' in x['text'] for x in view['text_matches'])
    assert not any(x['href_path'].startswith('/item/') for x in view['elements'])


def test_many_headings_do_not_crowd_out_complete_content_scope():
    view = compact_dom('<h2>Heading</h2>' * 30 +
                       '<div id="document-content"><p>Related prose</p></div>',
                       focus_terms=['Related'])
    assert any(r['id'] == 'document-content' for r in view['content_regions'])
