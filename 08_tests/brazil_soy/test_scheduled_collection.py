from io import BytesIO
from pathlib import Path
import json
import pytest
from PIL import Image
from agri_research_agent.pipelines.conab_growth_chart import read_growth_chart
from agri_research_agent.pipelines.brazil_soy_scheduled import report_links,weekly_assets,parse_growth

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('day,expected',[
    ('20261002',{'EMERGENCE':64.1,'VEGETATIVE':35.9}),
    ('20261009',{'EMERGENCE':43.,'VEGETATIVE':57.}),
    ('20261016',{'EMERGENCE':52.2,'VEGETATIVE':47.8})])
def test_printed_official_values_not_slice_geometry(day,expected):
    values=read_growth_chart((ROOT/'fixtures/conab-growth'/f'{day}.png').read_bytes())
    assert values==expected
    assert 'FLOWERING' not in values  # An unprinted stage is never zero-filled.

def test_blank_or_unknown_font_does_not_publish():
    buffer=BytesIO();Image.new('RGB',(1040,1070),'white').save(buffer,format='PNG')
    with pytest.raises(ValueError):read_growth_chart(buffer.getvalue())

def test_unknown_legend_does_not_relabel_stages():
    image=Image.open(ROOT/'fixtures/conab-growth/20261016.png').convert('RGB')
    image.paste('white',(0,930,image.width,image.height))
    buffer=BytesIO();image.save(buffer,format='PNG')
    with pytest.raises(ValueError):read_growth_chart(buffer.getvalue())

def test_official_publication_is_distinct_from_reporting_period():
    raw=b'''<span class="documentPublished">Publicado em <span>19/10/2026 19h00</span></span>
    <a href="/conab/report/plantio">Plantio e Colheita 10-10 a 16-10</a>
    <a href="/conab/report/feno">Fenologia das Lavouras 10-10 a 16-10</a>'''
    pub,assets=weekly_assets(raw,'https://www.gov.br/conab/report')
    assert pub=='2026-10-19' and len(assets)==2

@pytest.mark.parametrize('raw',[
    b'<a href="https://evil.example/report">Plantio e Colheita</a>',
    b'<span class="documentPublished">2026-10-19</span>',
    b'<span class="documentPublished">19/10/2026</span><a href="/conab/a">Plantio e Colheita</a>'
])
def test_unapproved_or_incomplete_assets_are_rejected(raw):
    with pytest.raises(ValueError):weekly_assets(raw,'https://www.gov.br/conab/')

def test_index_uses_official_period_and_rejects_external_host():
    rows=report_links(b'<a href="/conab/w">Acompanhamento das Lavouras - 12/10 a 18/10/26</a>','https://www.gov.br/conab/')
    assert rows[0][1].isoformat()=='2026-10-18'
    with pytest.raises(ValueError):report_links(b'<a href="https://evil.example/w">Acompanhamento das Lavouras - 12/10 a 18/10/26</a>','https://www.gov.br/conab/')
