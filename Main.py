import os
import json
import re
import logging
import xmlrpc.client
from datetime import datetime, timedelta

from flask import (
    Flask, request, jsonify,
    redirect, url_for, flash, render_template, session
)
from flask_wtf.csrf import CSRFProtect
from markupsafe import escape
from dotenv import load_dotenv
from zeep import Client
import requests
from audit_log import log_event

# ===================================================
# Load environment variables from .env
# ===================================================
load_dotenv()

# ===================================================
# Flask App Configuration
# ===================================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32).hex())

# Custom Jinja2 filter for European number formatting (12.014,03)
@app.template_filter('eur')
def format_eur(value):
    try:
        num = float(value)
        formatted = f"{num:,.2f}"  # 12,014.03
        # Swap: comma->@, dot->comma, @->dot
        formatted = formatted.replace(',', '@').replace('.', ',').replace('@', '.')
        return formatted
    except (ValueError, TypeError):
        return value

# ===================================================
# CSRF Protection
# ===================================================
app.config['WTF_CSRF_HEADERS'] = ['X-CSRFToken']  # Accept CSRF token from AJAX headers
csrf = CSRFProtect(app)

# ===================================================
# Logging Configuration
# ===================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===================================================
# Odoo Configuration (from .env)
# ===================================================
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# ===================================================
# Clientify Configuration (from .env)
# ===================================================
CLIENTIFY_ENABLED = os.getenv("CLIENTIFY_ENABLED", "True").lower() in ("true", "1", "yes")
CLIENTIFY_API_KEY = os.getenv("CLIENTIFY_API_KEY")
CLIENTIFY_BASE_URL = os.getenv("CLIENTIFY_BASE_URL", "https://api.clientify.net/v1/")
TAG_NAME = os.getenv("TAG_NAME", "Expodental-2026")

# ===================================================
# Odoo Sales Configuration (from .env)
# ===================================================
SALES_TEAM_ID = int(os.getenv("SALES_TEAM_ID", "22"))
PRICELIST_ORDER_ID = int(os.getenv("PRICELIST_ORDER_ID", "48"))
PRICELIST_MAYORISTA_ID = int(os.getenv("PRICELIST_MAYORISTA_ID", "32"))
PRICELIST_DEFAULT_ID = int(os.getenv("PRICELIST_DEFAULT_ID", "33"))

# Partner Tags
TAG_MAYORISTA_ID = int(os.getenv("TAG_MAYORISTA_ID", "2"))
TAG_CLINICA_DENTAL_ID = int(os.getenv("TAG_CLINICA_DENTAL_ID", "3"))
TAG_LABORATORIO_ID = int(os.getenv("TAG_LABORATORIO_ID", "4"))
TAG_ESTUDIANTE_ID = int(os.getenv("TAG_ESTUDIANTE_ID", "5"))
TAG_OTROS_ID = int(os.getenv("TAG_OTROS_ID", "15"))
TAG_FORMACION_ID = int(os.getenv("TAG_FORMACION_ID", "496"))
TAG_PROVEEDOR_ID = int(os.getenv("TAG_PROVEEDOR_ID", "17"))
TAG_INSTITUCION_ID = int(os.getenv("TAG_INSTITUCION_ID", "532"))
MANDATORY_TAG_NAMES = [x.strip() for x in os.getenv("MANDATORY_TAG_NAMES", "Expodental-2026").split(",")]
TAG_CONFIRMED_ORDER_ID = int(os.getenv("TAG_CONFIRMED_ORDER_ID", "977"))

# Warehouse & Payment
WAREHOUSE_ID = int(os.getenv("WAREHOUSE_ID", "19"))
PAYMENT_TERM_CASH_ID = int(os.getenv("PAYMENT_TERM_CASH_ID", "33"))
PAYMENT_TERM_CARD_ID = int(os.getenv("PAYMENT_TERM_CARD_ID", "34"))
EMAIL_TEMPLATE_ID = int(os.getenv("EMAIL_TEMPLATE_ID", "162"))
CAMPAIGN_NAME = os.getenv("CAMPAIGN_NAME", "Expodental 2026")
SOURCE_NAME = os.getenv("SOURCE_NAME", "FERIA")
ACTIVITY_USER_EMAIL = os.getenv("ACTIVITY_USER_EMAIL", "joana@bader.es")
ACTIVITY_PICKING_USER_EMAIL = os.getenv("ACTIVITY_PICKING_USER_EMAIL", "eva@bader.es")
BADER_WAREHOUSE_ID = int(os.getenv("BADER_WAREHOUSE_ID", "1"))
BADER_CARRIER_ID = int(os.getenv("BADER_CARRIER_ID", "57"))

# ===================================================
# SSL Configuration (from .env, optional for local dev)
# ===================================================
SSL_CERT = os.getenv("SSL_CERT_PATH")
SSL_KEY = os.getenv("SSL_KEY_PATH")




# ===================================================
# Input Validation Helpers
# ===================================================
def validate_email(email):
    """Validates basic email format."""
    if not email:
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def sanitize_string(value):
    """Strips and escapes a string input."""
    if not value:
        return ""
    return str(escape(value.strip()))


# ===================================================
# Odoo Helper
# ===================================================
def odoo_authenticate():
    """Autentica no Odoo e retorna o UID."""
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        return common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
    except Exception as e:
        logger.error(f"Erro ao autenticar no Odoo: {e}")
        return None


# ===================================================
# Clientify Helper
# ===================================================
def create_or_update_client_in_clientify(client_data):
    """Cria ou atualiza um cliente no Clientify com origem 'Expodental 2026', tipo baseado em customer_tag e tags apropriadas."""

    if not CLIENTIFY_ENABLED:
        logger.info("⚠️ Clientify desativado (.env CLIENTIFY_ENABLED=False). Operação ignorada.")
        return None, None

    headers = {
        "Authorization": f"Token {CLIENTIFY_API_KEY}",
        "Content-Type": "application/json"
    }

    # 🔁 Mapeamento dos nomes internos para os nomes esperados no Clientify
    tipo_display_map = {
        "mayorista": "Mayorista",
        "laboratorio": "Laboratorio Dental",
        "clinica_dental": "Clinica Dental",
        "estudiante": "Estudiante de Odontologia",
        "otros": "Otros"
    }

    tag_reconhecida = None
    client_id = None

    search_url = f"{CLIENTIFY_BASE_URL}contacts/?search={client_data['email']}"
    response = requests.get(search_url, headers=headers)

    if response.status_code == 200 and response.json().get("results"):
        exact_match = next(
            (c for c in response.json()["results"] if c.get("email", "").lower() == client_data["email"].lower()),
            None
        )

        if exact_match:
            client_id = exact_match["id"]
            existing_tags = exact_match.get("tags", [])

            for tag in existing_tags:
                if tag in ["mayorista", "laboratorio", "clinica_dental", "estudiante", "otros"]:
                    tag_reconhecida = tag
                    break

            novas_tags = existing_tags.copy()
            if "Expodental 2026" not in novas_tags:
                novas_tags.append("Expodental 2026")

            tipo = client_data.get("customer_tag")
            if tipo and tipo not in novas_tags and tipo in ["mayorista", "laboratorio", "clinica_dental", "estudiante", "otros"]:
                novas_tags.append(tipo)
                tag_reconhecida = tipo

            update_url = f"{CLIENTIFY_BASE_URL}contacts/{client_id}/"

            update_data = {
                "tags": novas_tags,
                "contact_source": "Expodental 2026"
            }

            # 🔄 Adiciona o tipo formatado se existir no mapeamento
            if tipo in tipo_display_map:
                update_data["contact_type"] = tipo_display_map[tipo]

            update_response = requests.patch(update_url, headers=headers, json=update_data)

            if update_response.status_code in [200, 204]:
                logger.info(f"✅ Cliente {client_id} atualizado com origem e tags.")
            else:
                logger.error(f"❌ Erro ao atualizar cliente: {update_response.text}")

            return tag_reconhecida, client_id

    # Cliente não existe → criar novo
    tags = [TAG_NAME, "Expodental 2026"]
    tipo = client_data.get("customer_tag")
    if tipo and tipo in ["mayorista", "laboratorio", "clinica_dental", "estudiante", "otros"]:
        tags.insert(0, tipo)
        tag_reconhecida = tipo

    contact_type_clientify = tipo_display_map.get(tipo) if tipo in tipo_display_map else None

    create_url = f"{CLIENTIFY_BASE_URL}contacts/"
    data = {
        "name": client_data["name"],
        "email": client_data["email"],
        "phone": client_data["phone"],
        "company": client_data.get("company") if isinstance(client_data.get("company"), str) and client_data.get("company").strip() else client_data.get("name"),
        "tags": tags,
        "contact_source": "Expodental 2026"
    }

    if contact_type_clientify:
        data["contact_type"] = contact_type_clientify

    create_response = requests.post(create_url, headers=headers, json=data)

    if create_response.status_code in [200, 201]:
        client_id = create_response.json()["id"]
        logger.info(f"🆕 Cliente criado com ID {client_id} e origem Expodental 2026.")
        return tag_reconhecida, client_id
    else:
        logger.error(f"❌ Erro ao criar cliente: {create_response.text}")
        return None, None


# ===================================================
# Application Routes
# ===================================================
@app.route('/')
def index():
    return render_template('home.html')


@app.route('/search', methods=['POST'])
def search():
    query = request.form.get('query', '').strip()

    if not query:
        flash('Debe introducir un NIF o email para buscar.', 'warning')
        return redirect(url_for('index'))

    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

    if not uid:
        return jsonify(error="Authentication failed")

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

    # ===================================================
    # Normalização inteligente do NIF
    # ===================================================
    # Códigos de país da UE válidos para NIF
    EU_COUNTRY_CODES = [
        'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR',
        'DE', 'GR', 'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL',
        'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE', 'EL'
    ]

    # Mapeamento completo código país → nome do país
    COUNTRY_CODE_TO_NAME = {
        'AT': 'Austria', 'BE': 'Belgium', 'BG': 'Bulgaria', 'HR': 'Croatia',
        'CY': 'Cyprus', 'CZ': 'Czech Republic', 'DK': 'Denmark', 'EE': 'Estonia',
        'FI': 'Finland', 'FR': 'France', 'DE': 'Germany', 'GR': 'Greece', 'EL': 'Greece',
        'HU': 'Hungary', 'IE': 'Ireland', 'IT': 'Italy', 'LV': 'Latvia',
        'LT': 'Lithuania', 'LU': 'Luxembourg', 'MT': 'Malta', 'NL': 'Netherlands',
        'PL': 'Poland', 'PT': 'Portugal', 'RO': 'Romania', 'SK': 'Slovakia',
        'SI': 'Slovenia', 'ES': 'Spain', 'SE': 'Sweden'
    }

    # Limpar input: remover espaços, hífens, pontos
    clean_query = re.sub(r'[\s.\-]', '', query).upper()

    # Detectar se o query é email ou NIF
    is_email = '@' in query

    # Variantes de busca para o NIF
    vat_variants = []
    country_code = None
    vat_number = None

    if not is_email:
        # Verificar se tem prefixo de país
        if len(clean_query) > 2 and clean_query[:2].isalpha() and clean_query[:2] in EU_COUNTRY_CODES:
            country_code = clean_query[:2]
            vat_number = clean_query[2:]
            # Buscar com prefixo E sem prefixo
            vat_variants = [clean_query, vat_number]
        elif len(clean_query) > 2 and clean_query[:2].isalpha() and clean_query[:2] == 'EL':
            # Grécia usa EL na VIES mas GR em alguns sistemas
            country_code = 'EL'
            vat_number = clean_query[2:]
            vat_variants = [clean_query, f'GR{vat_number}', vat_number]
        else:
            # Sem prefixo — assumir ES e buscar com e sem
            country_code = 'ES'
            vat_number = clean_query
            vat_variants = [clean_query, f'ES{clean_query}']
        
        logger.info(f"🔍 Busca NIF: input='{query}' → variantes={vat_variants}, país={country_code}")

    # Construir domain de busca inteligente
    if is_email:
        domain = [('email', 'ilike', query)]
    else:
        # Buscar por TODAS as variantes do NIF + email
        vat_conditions = ['|'] * (len(vat_variants)) if len(vat_variants) > 1 else []
        for v in vat_variants:
            vat_conditions.append(('vat', 'ilike', v))
        vat_conditions.append(('email', 'ilike', query))
        domain = vat_conditions

    partner_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                   'res.partner', 'search', [domain])
    
    # Construir o VAT normalizado (sempre com prefixo de país)
    normalized_vat = f"{country_code}{vat_number}" if country_code and vat_number else clean_query

    partner_data = {
        "name": "",
        "street": "",
        "city": "",
        "zip": "",
        "state": "",
        "country": "",
        "phone": "",
        "mobile": "",
        "email": "",
        "vat": normalized_vat if not is_email else "",
        "customer_tag": ""
    }

    if partner_ids:
        # ✅ Ler todos os parceiros encontrados com campos necessários
        partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                    'res.partner', 'read', [partner_ids],
                                    {'fields': ['name', 'street', 'city', 'zip', 'state_id', 'country_id',
                                                'phone', 'mobile', 'email', 'vat', 'x_sector', 'is_company',
                                                'category_id', 'partner_sector_id']})

        # ✅ Priorizar: Company primeiro, Individual só se não houver Company
        company_partners = [p for p in partners if p.get('is_company')]
        partner = company_partners[0] if company_partners else partners[0]

        # Helper: Odoo retorna False para campos vazios em vez de ""
        def safe(val, default=""):
            return val if val and val is not False else default

        state_name = partner.get('state_id', ["", ""])[1].split(' (')[0] if isinstance(partner.get('state_id'), list) else ""
        country_name = partner.get('country_id', ["", ""])[1] if isinstance(partner.get('country_id'), list) else ""
        
        # Garantir que o VAT tem sempre o prefixo de país
        existing_vat = safe(partner.get("vat"))
        if existing_vat and len(existing_vat) > 2 and not existing_vat[:2].isalpha():
            existing_vat = f"ES{existing_vat}"
        
        session['partner_id'] = partner['id']

        # ✅ Sector: ler de partner_sector_id (Area do Odoo) como primário, x_sector como fallback
        # Mapeamento: nome do sector → valor do dropdown
        sector_name_to_dropdown = {
            'Clínica Dental': 'clinica',
            'Clinica Dental': 'clinica',
            'Laboratorio Dental': 'laboratorio',
            'Estudiante de Odontología': 'estudiante',
            'Estudiante de Odontologia': 'estudiante',
            'Centro de Formación': 'formacion',
            'Centro de Formacion': 'formacion',
            'Mayorista': 'deposito',
            'Otro sector': 'fuera',
            'Otro Sector': 'fuera',
            'Proveedor': 'proveedor',
            'Institución': 'institución',
            'Institucion': 'institución',
            'Servicios Técnicos': 'servicios tecnicos',
            'Servicios Tecnicos': 'servicios tecnicos',
        }
        customer_tag = ''
        # Prioridade 1: partner_sector_id (campo 'Area' do Odoo)
        sector_data = partner.get('partner_sector_id')
        if sector_data and sector_data is not False:
            sector_name = sector_data[1] if isinstance(sector_data, (list, tuple)) else str(sector_data)
            customer_tag = sector_name_to_dropdown.get(sector_name, '')
            logger.info(f"🏷️ Sector lido de partner_sector_id: '{sector_name}' → dropdown='{customer_tag}'")
        # Prioridade 2: x_sector (fallback)
        if not customer_tag:
            customer_tag = safe(partner.get('x_sector'))
            if customer_tag:
                logger.info(f"🏷️ Sector lido de x_sector (fallback): '{customer_tag}'")

        partner_data = {
            "name": safe(partner.get("name")),
            "street": safe(partner.get('street')),
            "city": safe(partner.get('city')),
            "zip": safe(partner.get("zip")),
            "state": state_name,
            "country": country_name,
            "phone": safe(partner.get("phone")),
            "mobile": safe(partner.get("mobile")),
            "email": safe(partner.get("email")),
            "vat": existing_vat,
            "customer_tag": customer_tag
        }

        # 📋 Audit: registar busca
        log_event('SEARCH', {
            'query': query,
            'partner_id': partner['id'],
            'partner_name': safe(partner.get('name')),
            'vat': existing_vat,
            'sector': customer_tag
        })
    else:
        # Se não encontrar, tenta buscar na API VIES
        if not is_email and country_code and vat_number:
            # Para VIES, Grécia usa EL
            vies_country = country_code if country_code != 'GR' else 'EL'
            client = Client('http://ec.europa.eu/taxation_customs/vies/checkVatService.wsdl')
            logger.info(f"🌐 Consulta VIES: país={vies_country}, nif={vat_number}")
            try:
                response = client.service.checkVat(vies_country, vat_number)

                if response.valid:
                    address_parts = response.address.split('\n')
                    street_and_number = address_parts[0].strip() if len(address_parts) > 0 else ""
                    city_and_zip = address_parts[1].strip() if len(address_parts) > 1 else ""
                    city = city_and_zip.split('\n')[0].strip() if city_and_zip else ""
                    city_zip_part = address_parts[-1].strip() if len(address_parts) > 2 else ""
                    zip_code = city_zip_part.split(' ')[0] if city_zip_part else ""

                    full_country_name = COUNTRY_CODE_TO_NAME.get(country_code, "")

                    partner_data.update({
                        "name": response.name.strip(),
                        "street": street_and_number,
                        "city": city,
                        "zip": zip_code,
                        "country": full_country_name,
                        "state": city,
                        "vat": normalized_vat
                    })
                    logger.info(f"✅ Dados VIES: {partner_data['name']} ({full_country_name})")
                    flash('Datos cargados desde la API VIES.', 'success')
                else:
                    logger.warning("VAT não válido ou não encontrado na API VIES.")
                    flash('VAT no válido o no encontrado en la API VIES.', 'warning')
            except Exception as e:
                logger.error(f"Erro ao consultar a API VIES: {e}")
                flash(f'Error al consultar la API VIES: {str(e)}', 'error')

    # ✅ buscar todos os países da base do Odoo (com código para filtragem)
    all_countries = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.country', 'search_read', [[], ['name', 'code']], {'limit': 999, 'context': {'lang': 'es_ES'}})

    # ✅ Países prioritários por código (ES, PT, IT, FR) — evita duplicados
    priority_codes = ['ES', 'PT', 'IT', 'FR']
    priority_countries = [c for c in all_countries if c.get('code') in priority_codes]
    other_countries = [c for c in all_countries if c.get('code') not in priority_codes]
    # Ordenar priority na ordem desejada
    priority_order = {code: i for i, code in enumerate(priority_codes)}
    priority_countries.sort(key=lambda c: priority_order.get(c.get('code'), 99))

    return render_template('search.html', data=partner_data, priority_countries=priority_countries, other_countries=other_countries)


@app.route('/save', methods=['POST'])
def save():
    # Coletar os dados do formulário
    name = request.form.get('name', '').strip()
    vat = request.form.get('vat', '').strip()

    # Normalizar VAT: limpar e garantir prefixo de país
    if vat:
        vat = re.sub(r'[\s.\-]', '', vat).upper()
        # Se não tem prefixo de país válido, adicionar "ES"
        EU_PREFIXES = {'AT','BE','BG','HR','CY','CZ','DK','EE','FI','FR','DE','GR','EL','HU','IE','IT','LV','LT','LU','MT','NL','PL','PT','RO','SK','SI','ES','SE'}
        if len(vat) >= 3 and (not vat[:2].isalpha() or vat[:2] not in EU_PREFIXES):
            vat = 'ES' + vat
            logger.info(f"VAT normalizado com prefixo ES: {vat}")

    email = request.form.get('email', '').strip()
    street = request.form.get('street', '').strip()
    city = request.form.get('city', '').strip()
    state_name = request.form.get('state', '').strip()
    country_name = request.form.get('country', '').strip()
    phone = request.form.get('phone', '').strip()
    mobile = request.form.get('mobile', '').strip()
    zip_code = request.form.get('zip', '').strip()
    customer_tag = request.form.get('customer_tag', '').strip()

    # ✅ Validação de inputs
    if not name:
        flash('El nombre es obligatorio.', 'error')
        return redirect(url_for('index'))

    if email and not validate_email(email):
        flash('Formato de email no válido.', 'error')
        return redirect(url_for('index'))

    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

    if not uid:
        return jsonify(error="Authentication failed")

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

    # Buscar IDs do estado e do país no Odoo (com contexto es_ES porque os nomes vêm em espanhol)
    state_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.country.state', 'search', [[('name', '=', state_name)]], {'limit': 1, 'context': {'lang': 'es_ES'}})
    country_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.country', 'search', [[('name', '=', country_name)]], {'limit': 1, 'context': {'lang': 'es_ES'}})

    # ✅ Mapeamento: x_sector → nome da etiqueta no Odoo
    sector_to_tag_name = {
        "clinica": "CLINICA DENTAL",
        "laboratorio": "LABORATORIO DENTAL",
        "estudiante": "ESTUDIANTE",
        "deposito": "MAYORISTA",
        "formacion": "Centro de Formacion",
        "fuera": "OTROS",
        "proveedor": "PROVEEDOR",
        "institución": "INSTITUCION",
        "servicios tecnicos": "SERVICIO TÉCNICO"
    }

    # ✅ Mapeamento: dropdown value → nome do partner_sector_id (res.partner.sector)
    dropdown_to_sector_name = {
        "clinica": "Clínica Dental",
        "laboratorio": "Laboratorio Dental",
        "estudiante": "Estudiante de Odontología",
        "formacion": "Centro de Formación",
        "deposito": "Mayorista",
        "fuera": "Otro sector",
        "proveedor": "Proveedor",
        "institución": "Institución",
        "servicios tecnicos": "Servicios Técnicos"
    }

    # Buscar partner_sector_id pelo nome
    partner_sector_id = False
    sector_label = dropdown_to_sector_name.get(customer_tag)
    if sector_label:
        sector_search = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner.sector', 'search',
            [[('name', '=', sector_label)]], {'limit': 1})
        if sector_search:
            partner_sector_id = sector_search[0]
            logger.info(f"🏷️ partner_sector_id encontrado: '{sector_label}' → ID={partner_sector_id}")
        else:
            logger.warning(f"🏷️ partner_sector_id não encontrado para '{sector_label}'")

    # ✅ Buscar ou criar a etiqueta do sector por nome (nunca duplicar)
    sector_tag_name = sector_to_tag_name.get(customer_tag)
    tag_id = None
    if sector_tag_name:
        tag_search = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner.category', 'search',
            [[('name', '=', sector_tag_name)]], {'limit': 1})
        if tag_search:
            tag_id = tag_search[0]
            logger.info(f"✅ Etiqueta sector '{sector_tag_name}' encontrada: ID={tag_id}")
        else:
            tag_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner.category', 'create',
                [{'name': sector_tag_name}])
            logger.info(f"✅ Etiqueta sector '{sector_tag_name}' criada: ID={tag_id}")

    # Buscar IDs das etiquetas obrigatórias por nome (configuradas via .env)
    mandatory_tags = []
    for tag_name in MANDATORY_TAG_NAMES:
        tag_search = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner.category', 'search', [[('name', '=', tag_name)]], {'limit': 1})
        if tag_search:
            mandatory_tags.append(tag_search[0])
        else:
            new_tag = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner.category', 'create', [{'name': tag_name}])
            mandatory_tags.append(new_tag)
            logger.info(f"✅ Etiqueta '{tag_name}' criada no Odoo com ID {new_tag}")

    # Lista final de etiquetas a serem associadas ao cliente
    tag_ids = ([tag_id] if tag_id else []) + mandatory_tags

    # Definir a tarifa (pricelist_id) com base no sector — apenas 'deposito' (mayorista) usa tarifa diferente
    pricelist_id = PRICELIST_MAYORISTA_ID if customer_tag == 'deposito' else PRICELIST_DEFAULT_ID

    # Helper: Buscar user_id da Joana para atividades
    def get_activity_user_id():
        try:
            user_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.users', 'search',
                [[('login', '=', ACTIVITY_USER_EMAIL)]], {'limit': 1})
            return user_ids[0] if user_ids else uid
        except Exception:
            return uid

    # Helper: Criar atividade no contacto
    def create_activity(partner_id, summary, note_html):
        try:
            activity_user_id = get_activity_user_id()
            deadline = (datetime.now() + timedelta(days=15)).strftime('%Y-%m-%d')

            # Buscar ID do tipo de atividade "To Do"
            activity_type = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mail.activity.type', 'search',
                [[('name', 'ilike', 'To Do')]], {'limit': 1})
            activity_type_id = activity_type[0] if activity_type else 4  # fallback to generic

            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mail.activity', 'create', [{
                'res_model_id': models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'ir.model', 'search',
                    [[('model', '=', 'res.partner')]], {'limit': 1})[0],
                'res_id': partner_id,
                'activity_type_id': activity_type_id,
                'summary': summary,
                'note': note_html,
                'user_id': activity_user_id,
                'date_deadline': deadline,
            }])
            logger.info(f"✅ Atividade criada para {ACTIVITY_USER_EMAIL} no contacto {partner_id}: {summary}")
        except Exception as e:
            logger.error(f"Erro ao criar atividade: {e}")

    # Verificar se o cliente já existe no Odoo
    partner_domain = ['|', ('vat', '=', vat), ('email', '=', email)]
    existing_partner_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search', [partner_domain])

    # ✅ Verificar duplicados de NIF
    if vat and len(vat) >= 4:
        nif_duplicates = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search_read',
            [[('vat', '=', vat)]], {'fields': ['id', 'name', 'email', 'vat']})
        if len(nif_duplicates) > 1:
            dup_lines = []
            for dup in nif_duplicates:
                dup_lines.append(f"<li><b>ID {dup['id']}</b> - {dup['name']} ({dup.get('email', 'sin email')})</li>")
            dup_html = f"<p>Se encontraron <b>{len(nif_duplicates)} contactos</b> con el mismo NIF <b>{vat}</b>:</p><ul>{''.join(dup_lines)}</ul><p>Por favor, revisar y fusionar si es necesario.</p>"
            # Criar atividade no primeiro contacto
            create_activity(nif_duplicates[0]['id'], f"⚠️ NIF Duplicado: {vat} ({len(nif_duplicates)} contactos)", dup_html)

    if existing_partner_ids:
        # Atualizar cliente existente — preservar etiquetas e registar mudanças no chatter
        partner_id = existing_partner_ids[0]
        try:
            # Ler dados atuais do parceiro para comparar mudanças
            old_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'read', [partner_id],
                {'fields': ['name', 'vat', 'email', 'street', 'city', 'phone', 'mobile', 'zip', 'x_sector']})[0]

            # Construir lista de mudanças
            changes = []
            field_labels = {
                'name': 'Nombre', 'vat': 'NIF', 'email': 'Email', 'street': 'Calle',
                'city': 'Ciudad', 'phone': 'Teléfono', 'mobile': 'Celular', 'zip': 'Código Postal', 'x_sector': 'Sector'
            }
            new_values = {
                'name': name, 'vat': vat, 'email': email, 'street': street,
                'city': city, 'phone': phone, 'mobile': mobile, 'zip': zip_code, 'x_sector': customer_tag
            }
            for field_key, label in field_labels.items():
                old_val = str(old_data.get(field_key) or '').strip()
                new_val = str(new_values.get(field_key) or '').strip()
                if old_val != new_val:
                    changes.append(f"<li><b>{label}:</b> {old_val or '(vacío)'} → {new_val or '(vacío)'}</li>")

            # Atualizar etiquetas — remover etiqueta de sector antiga e adicionar a nova, preservar as demais
            # Buscar IDs de todas as tags de sector conhecidas (para saber quais remover)
            all_sector_tag_ids = set()
            for sname in sector_to_tag_name.values():
                found = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner.category', 'search',
                    [[('name', '=', sname)]], {'limit': 1})
                if found:
                    all_sector_tag_ids.add(found[0])
            logger.info(f"🏷️ Tags de sector conhecidas: {all_sector_tag_ids}")

            # Ler etiquetas existentes do parceiro
            partner_tags_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'read', [partner_id],
                {'fields': ['category_id']})[0]
            current_tag_ids = partner_tags_data.get('category_id', [])
            logger.info(f"🏷️ Tags atuais do parceiro {partner_id}: {current_tag_ids}")
            logger.info(f"🏷️ Novo tag_id do sector: {tag_id}, tag_ids a adicionar: {tag_ids}")

            # Construir operações de etiquetas:
            # 1. Remover etiquetas de sector antigas (3 = unlink)
            # 2. Adicionar a nova etiqueta de sector + obrigatórias (4 = link)
            category_ops = []
            for old_tid in current_tag_ids:
                if old_tid in all_sector_tag_ids and old_tid != tag_id:
                    category_ops.append((3, old_tid))  # Remover sector antigo
                    logger.info(f"🏷️ REMOVER tag antiga: ID={old_tid}")
            for tid in tag_ids:
                if tid not in current_tag_ids:
                    category_ops.append((4, tid))  # Adicionar novo
                    logger.info(f"🏷️ ADICIONAR tag nova: ID={tid}")

            logger.info(f"🏷️ Operações finais category_id: {category_ops}")

            # Atualizar dados
            update_data = {
                'name': name,
                'vat': vat if vat and len(vat) >= 4 else False,
                'email': email,
                'street': street,
                'city': city,
                'state_id': state_id[0] if state_id else False,
                'country_id': country_id[0] if country_id else False,
                'phone': phone,
                'mobile': mobile,
                'zip': zip_code,
                'property_product_pricelist': pricelist_id,
                'x_sector': customer_tag or False,
                'partner_sector_id': partner_sector_id
            }
            if category_ops:
                update_data['category_id'] = category_ops
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'write', [existing_partner_ids, update_data])

            # 📋 Audit: registar atualização de cliente
            log_event('CLIENT_UPDATE', {
                'partner_id': partner_id,
                'name': name,
                'vat': vat,
                'email': email,
                'phone': phone,
                'mobile': mobile,
                'street': street,
                'city': city,
                'state': state_name,
                'country': country_name,
                'zip': zip_code,
                'sector': customer_tag,
                'changes': changes
            })

            # Postar mudanças no chatter do contacto
            if changes:
                change_html = f"<div style='font-size:14px;'><b>📝 Datos actualizados desde Mobil Feira:</b><ul>{''.join(changes)}</ul></div>"
                try:
                    models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'message_post', [partner_id], {
                        'body': change_html,
                        'message_type': 'comment',
                        'subtype_xmlid': 'mail.mt_note',
                    })
                    logger.info(f"✅ Mudanças registadas no chatter do contacto {partner_id}")
                except Exception as e:
                    logger.error(f"Erro ao postar mudanças no chatter: {e}")

                # Criar atividade para Joana revisar as mudanças
                activity_note = f"<p>Se actualizaron datos del contacto desde Mobil Feira:</p><ul>{''.join(changes)}</ul>"
                create_activity(partner_id, "📝 Revisar cambios de datos - Mobil Feira", activity_note)

            session['partner_id'] = partner_id
            flash('Datos del cliente actualizados con éxito!', 'success')
        except Exception as e:
            logger.error(f"Erro ao atualizar cliente no Odoo: {e}")
            session['partner_id'] = existing_partner_ids[0]
            flash(f'Advertencia: Cliente guardado parcialmente. Error: {str(e)}', 'warning')
    else:
        try:
            new_partner_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'create', [{
                'name': name,
                'vat': vat if vat and len(vat) >= 4 else False,
                'email': email,
                'street': street,
                'city': city,
                'state_id': state_id[0] if state_id else False,
                'country_id': country_id[0] if country_id else False,
                'phone': phone,
                'mobile': mobile,
                'zip': zip_code,
                'category_id': [(6, 0, tag_ids)],
                'property_product_pricelist': pricelist_id,
                'x_sector': customer_tag or False,
                'partner_sector_id': partner_sector_id
            }])

            if new_partner_id:
                session['partner_id'] = new_partner_id

                # 📋 Audit: registar criação de cliente
                log_event('CLIENT_CREATE', {
                    'partner_id': new_partner_id,
                    'name': name,
                    'vat': vat,
                    'email': email,
                    'phone': phone,
                    'mobile': mobile,
                    'street': street,
                    'city': city,
                    'state': state_name,
                    'country': country_name,
                    'zip': zip_code,
                    'sector': customer_tag
                })
                return redirect(url_for('create_presupuesto', vat=vat))
            else:
                flash('Error al crear el cliente.', 'error')
                return redirect(url_for('index'))
        except Exception as e:
            logger.error(f"Erro ao criar cliente no Odoo: {e}")
            flash(f'Error al crear el cliente: {str(e)}', 'error')
            return redirect(url_for('index'))

    # Existing client was updated — verify session has correct partner_id
    if not session.get('partner_id'):
        logger.error("BUG PREVENTION: No partner_id in session after save!")
        flash('Error: No se pudo identificar el cliente. Busque de nuevo.', 'error')
        return redirect(url_for('index'))
    return redirect(url_for('create_presupuesto', vat=vat))


@app.route('/create_presupuesto', methods=['GET'])
def create_presupuesto():
    vat = request.args.get('vat')
    partner_id = session.get('partner_id')

    if not partner_id:
        flash('Error: No se encontró el cliente en la sesión. Por favor, busque el cliente primero.', 'error')
        return redirect(url_for('index'))

    if not vat or len(vat) < 3:
        flash('NIF/VAT inválido.', 'error')
        return redirect(url_for('index'))

    country_code = vat[:2].upper()
    number = vat[2:]

    # Defina o ID de posição fiscal padrão
    fiscal_position_id = 1

    if country_code == 'ES' and len(vat) > 2 and vat[2].isdigit():
        pass  # Mantenha o fiscal_position_id padrão para NIF espanhol
    elif country_code != 'ES':
        client = Client('http://ec.europa.eu/taxation_customs/vies/checkVatService.wsdl')
        try:
            result = client.service.checkVat(country_code, number)
            if result['valid']:
                fiscal_position_id = 4  # intracomunitário sem IVA
            else:
                flash('VAT inválido. Será tratado com IVA nacional.', 'warning')
                fiscal_position_id = 1
        except Exception as e:
            logger.warning(f"Erro ao consultar VIES: {e}. Aplicando IVA padrão.")
            flash('Error al consultar VAT. Se tratará con IVA nacional.', 'warning')
            fiscal_position_id = 1

    # Autentique com o Odoo
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return redirect(url_for('index'))

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

    # ✅ Verificar se já existe um presupuesto em rascunho para este cliente (evitar duplicados)
    existing_orders = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'search_read',
        [[('partner_id', '=', partner_id), ('state', 'in', ['draft', 'sent']),
          ('client_order_ref', '=', 'Expodental 2026')]],
        {'fields': ['id', 'name', 'state'], 'limit': 1, 'order': 'create_date desc'})

    if existing_orders:
        existing_id = existing_orders[0]['id']
        existing_name = existing_orders[0]['name']
        logger.info(f"📋 Presupuesto existente encontrado: {existing_name} (ID={existing_id}) — não criar duplicado")
        flash(f'Ya existe un presupuesto ({existing_name}). Abriendo...', 'info')

        # 📋 Audit: presupuesto existente reutilizado
        log_event('PRESUPUESTO_REUSED', {
            'presupuesto_id': existing_id,
            'presupuesto_name': existing_name,
            'partner_id': partner_id
        })

        return redirect(url_for('presupuesto_details', presupuesto_id=existing_id))

    # Buscar campaign_id e source_id por nome
    campaign_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'utm.campaign', 'search', [[('name', '=', CAMPAIGN_NAME)]], {'limit': 1})
    if not campaign_ids:
        campaign_ids = [models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'utm.campaign', 'create', [{'name': CAMPAIGN_NAME}])]
        logger.info(f"✅ Campaña '{CAMPAIGN_NAME}' criada no Odoo com ID {campaign_ids[0]}")

    source_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'utm.source', 'search', [[('name', '=', SOURCE_NAME)]], {'limit': 1})
    if not source_ids:
        source_ids = [models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'utm.source', 'create', [{'name': SOURCE_NAME}])]
        logger.info(f"✅ Origen '{SOURCE_NAME}' criado no Odoo com ID {source_ids[0]}")

    try:
        presupuesto_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'create', [{
            'partner_id': partner_id,
            'pricelist_id': PRICELIST_ORDER_ID,
            'fiscal_position_id': fiscal_position_id,
            'client_order_ref': "Expodental 2026",
            'team_id': SALES_TEAM_ID,
            'campaign_id': campaign_ids[0],
            'source_id': source_ids[0]
        }])
    except Exception as e:
        logger.error(f"Erro ao criar presupuesto: {e}")
        flash(f'Error al crear el presupuesto: {str(e)}', 'error')
        return redirect(url_for('index'))

    if presupuesto_id:
        # 📋 Audit: registar criação de presupuesto
        log_event('PRESUPUESTO_CREATE', {
            'presupuesto_id': presupuesto_id,
            'partner_id': partner_id
        })
        flash('Presupuesto creado con éxito!', 'success')
        return redirect(url_for('presupuesto_details', presupuesto_id=presupuesto_id))
    else:
        flash('Error al crear el Presupuesto.', 'error')
        return redirect(url_for('index'))


@app.route('/presupuesto_details/<int:presupuesto_id>', methods=['GET', 'POST'])
def presupuesto_details(presupuesto_id):
    # Autenticação com o Odoo
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return redirect(url_for('index'))

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

    if request.method == 'POST':
        product_code = request.form.get('product_code', '').strip().lower()
        product_qty = int(request.form.get('product_qty', 1))

        if not product_code:
            flash('Debe introducir un código de producto.', 'warning')
        else:
            # Carregar preços personalizados de um arquivo JSON
            try:
                json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'price_list_feira.json')
                with open(json_path, 'r') as file:
                    custom_prices = json.load(file)
                    custom_prices_lower = {k.lower(): v for k, v in custom_prices.items()}
                logger.info(f"✅ price_list_feira.json carregado: {len(custom_prices_lower)} produtos")
            except FileNotFoundError:
                logger.warning("⚠️ price_list_feira.json não encontrado!")
                custom_prices_lower = {}

            custom_price = custom_prices_lower.get(product_code)
            logger.info(f"🔍 Produto '{product_code}' → preço custom: {custom_price}")

            # Busque o produto pelo seu código de referência
            product_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search', [[['default_code', 'ilike', product_code]]])

            if product_ids:
                product_id = product_ids[0]

                sale_order_line_vals = {
                    'order_id': presupuesto_id,
                    'product_id': product_id,
                    'product_uom_qty': product_qty
                }

                new_line_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'create', [sale_order_line_vals])

                # 📋 Audit: registar adição de produto
                log_event('PRODUCT_ADD', {
                    'presupuesto_id': presupuesto_id,
                    'product_code': product_code,
                    'product_id': product_id,
                    'qty': product_qty,
                    'line_id': new_line_id,
                    'custom_price': custom_price
                })

                # Forçar o preço custom APÓS criar a linha (evita que o onchange do Odoo sobrescreva)
                if new_line_id and custom_price is not None:
                    models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'write', [[new_line_id], {
                        'price_unit': custom_price
                    }])
                    logger.info(f"✅ Preço custom {custom_price}€ aplicado à linha {new_line_id}")

                if new_line_id:
                    flash('Producto añadido con éxito!', 'success')
                else:
                    flash('Error al añadir el producto.', 'error')
            else:
                flash('Producto no encontrado.', 'error')

    # Pegue os detalhes do Presupuesto pelo ID
    presupuesto_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', [presupuesto_id], {'context': {'lang': 'es_ES'}})[0]

    sale_order_lines = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'search_read', [[['order_id', '=', presupuesto_id]]], {'fields': ['product_id', 'name', 'product_uom_qty', 'price_unit', 'price_subtotal'], 'context': {'lang': 'es_ES'}})

    return render_template('presupuesto.html', presupuesto_data=presupuesto_data, presupuesto_id=presupuesto_id, sale_order_lines=sale_order_lines)


@app.route('/update_warehouse/<int:presupuesto_id>', methods=['POST'])
def update_warehouse(presupuesto_id):
    """Atualiza o armazém do presupuesto em tempo real via AJAX."""
    uid = odoo_authenticate()
    if not uid:
        return jsonify(error="Authentication failed"), 401

    warehouse_id = int(request.form.get('warehouse_id', WAREHOUSE_ID))
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

    try:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'write', [[presupuesto_id], {
            'warehouse_id': warehouse_id
        }])
        logger.info(f"✅ Armazém atualizado para ID {warehouse_id} no presupuesto {presupuesto_id}")
        return jsonify(success=True, warehouse_id=warehouse_id)
    except Exception as e:
        logger.error(f"❌ Falha ao modificar armazém: {e}")
        return jsonify(error=str(e)), 500


@app.route('/toggle_iva/<int:presupuesto_id>', methods=['POST'])
def toggle_iva(presupuesto_id):
    """Remove ou restaura o IVA de todas as linhas do presupuesto via AJAX."""
    uid = odoo_authenticate()
    if not uid:
        return jsonify(error="Authentication failed"), 401

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

    try:
        # Ler posição fiscal atual
        order_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', [presupuesto_id],
            {'fields': ['fiscal_position_id', 'amount_untaxed', 'amount_tax', 'amount_total']})[0]

        current_fp = order_data.get('fiscal_position_id')
        current_fp_id = current_fp[0] if isinstance(current_fp, list) else current_fp

        # Toggle: se a posição fiscal é 4 (sem IVA), restaurar para 1 (com IVA); senão, zerar
        if current_fp_id == 4:
            # RESTAURAR IVA — mudar posição fiscal para 1 (padrão com IVA)
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'write', [[presupuesto_id], {
                'fiscal_position_id': 1
            }])

            # Restaurar impostos nas linhas usando onchange
            line_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'search',
                [[('order_id', '=', presupuesto_id)]])

            for line_id in line_ids:
                line_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'read', [line_id],
                    {'fields': ['product_id']})[0]
                product_id = line_data['product_id'][0] if isinstance(line_data['product_id'], list) else line_data['product_id']

                # Buscar os impostos padrão do produto
                product_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read', [product_id],
                    {'fields': ['taxes_id']})[0]
                tax_ids = product_data.get('taxes_id', [])

                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'write', [[line_id], {
                    'tax_id': [(6, 0, tax_ids)]
                }])

            iva_active = True
            logger.info(f"✅ IVA restaurado no presupuesto {presupuesto_id}")
        else:
            # REMOVER IVA — mudar posição fiscal para 4 (sem IVA)
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'write', [[presupuesto_id], {
                'fiscal_position_id': 4
            }])

            # Remover impostos de todas as linhas
            line_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'search',
                [[('order_id', '=', presupuesto_id)]])

            for line_id in line_ids:
                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'write', [[line_id], {
                    'tax_id': [(5, 0, 0)]  # Remove todos os impostos
                }])

            iva_active = False
            logger.info(f"✅ IVA removido do presupuesto {presupuesto_id}")

        # Reler totais atualizados
        updated_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', [presupuesto_id],
            {'fields': ['amount_untaxed', 'amount_tax', 'amount_total']})[0]

        return jsonify(
            success=True,
            iva_active=iva_active,
            amount_untaxed=updated_data['amount_untaxed'],
            amount_tax=updated_data['amount_tax'],
            amount_total=updated_data['amount_total']
        )

    except Exception as e:
        logger.error(f"❌ Erro ao alternar IVA: {e}")
        return jsonify(error=str(e)), 500


@app.route('/update_product_line/<int:line_id>', methods=['POST'])
def update_product_line(line_id):
    """Atualiza a quantidade e o preço de uma linha de pedido via AJAX."""
    uid = odoo_authenticate()
    if not uid:
        return jsonify(error="Authentication failed"), 401

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

    try:
        qty = float(request.form.get('qty', 1))
        price = float(request.form.get('price', 0))

        # Primeiro atualizar a quantidade
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'write', [[line_id], {
            'product_uom_qty': qty
        }])

        # Depois forçar o preço (separado para evitar que o onchange sobrescreva)
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'write', [[line_id], {
            'price_unit': price
        }])

        logger.info(f"✅ Linha {line_id} atualizada: qty={qty}, price={price}€")
        return jsonify(success=True)

    except Exception as e:
        logger.error(f"❌ Erro ao atualizar linha {line_id}: {e}")
        return jsonify(error=str(e)), 500


@app.route('/confirm_presupuesto/<int:presupuesto_id>', methods=['POST'])
def confirm_presupuesto(presupuesto_id):
    """Confirma um orçamento no Odoo e atualiza o campo 'Valor Pedido' no Clientify."""
    uid = odoo_authenticate()
    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return redirect(url_for('index'))

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    presupuesto_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', [presupuesto_id], {'context': {'lang': 'es_ES'}})

    if not presupuesto_data:
        flash('Error al buscar el presupuesto.', 'error')
        return redirect(url_for('index'))

    presupuesto_data = presupuesto_data[0]
    total_pedido = presupuesto_data.get("amount_total", 0.0)

    partner_id = presupuesto_data["partner_id"][0]
    partner_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'read', [partner_id])[0]

    company_value = partner_data.get("company_name") or partner_data.get("name", "")
    if not isinstance(company_value, str):
        company_value = str(company_value)

    client_info = {
        "name": partner_data.get("name", ""),
        "email": partner_data.get("email", ""),
        "phone": partner_data.get("phone", ""),
        "company": company_value
    }

    # ✅ Recuperar customer_tag com base nas categorias no Odoo
    category_ids = partner_data.get("category_id", [])
    tag_map_reverse = {
        TAG_MAYORISTA_ID: "mayorista",
        TAG_CLINICA_DENTAL_ID: "clinica_dental",
        TAG_LABORATORIO_ID: "laboratorio",
        TAG_ESTUDIANTE_ID: "estudiante",
        TAG_OTROS_ID: "otros"
    }
    customer_tag = None
    for cat_id in category_ids:
        if cat_id in tag_map_reverse:
            customer_tag = tag_map_reverse[cat_id]
            break

    client_info["customer_tag"] = customer_tag

    # ✅ Atualizar armazém (selecionado pelo utilizador no formulário)
    # Ler o armazém do formulário OU do pedido já salvo no Odoo (se foi alterado via AJAX)
    wh_from_form = request.form.get('warehouse_id')
    if wh_from_form:
        selected_warehouse_id = int(wh_from_form)
    else:
        # Ler o armazém já definido no pedido (atualizado via AJAX)
        wh_data = presupuesto_data.get('warehouse_id')
        selected_warehouse_id = wh_data[0] if isinstance(wh_data, list) else int(wh_data or WAREHOUSE_ID)
    
    logger.info(f"📦 Armazém selecionado: {selected_warehouse_id} (Bader={BADER_WAREHOUSE_ID})")
    try:
        update_result = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'write', [[presupuesto_id], {
            'warehouse_id': selected_warehouse_id
        }])
        if update_result:
            logger.info(f"✅ Armazém atualizado para ID {selected_warehouse_id} no pedido {presupuesto_id}")
    except Exception as e:
        logger.error(f"❌ Falha ao modificar armazém: {e}")

    # 📋 Audit: registar confirmação de presupuesto
    log_event('PRESUPUESTO_CONFIRM', {
        'presupuesto_id': presupuesto_id,
        'partner_id': partner_id,
        'partner_name': partner_data.get('name'),
        'total': total_pedido,
        'warehouse_id': selected_warehouse_id,
        'order_ref': presupuesto_data.get('name')
    })

    # ✅ Criar ou atualizar cliente e capturar o ID
    if CLIENTIFY_ENABLED:
        _, clientify_id = create_or_update_client_in_clientify(client_info)

        # ✅ Atualizar o campo personalizado com total do pedido
        try:
            headers = {
                "Authorization": f"Token {CLIENTIFY_API_KEY}",
                "Content-Type": "application/json"
            }

            if clientify_id:
                update_url = f"{CLIENTIFY_BASE_URL}contacts/{clientify_id}/"
                update_data = {
                    "custom_fields": [
                        {
                            "field": "Valor Pedido",
                            "value": float(total_pedido)
                        }
                    ]
                }

                update_response = requests.patch(update_url, headers=headers, json=update_data)
                if update_response.status_code in [200, 204]:
                    logger.info(f"✅ Campo 'Valor Pedido' atualizado no Clientify para {total_pedido}€")
                else:
                    logger.error(f"❌ Erro ao atualizar campo personalizado: {update_response.text}")
            else:
                logger.warning(f"❌ Cliente não encontrado no Clientify para atualização.")

        except Exception as e:
            logger.error(f"❌ Erro ao atualizar campo personalizado no Clientify: {e}")
    else:
        logger.info("⚠️ Clientify desativado. Atualização de 'Valor Pedido' ignorada.")

    # ✅ Definir transportadora no pedido ANTES de confirmar (Bader = LOG Dentaltix ID 57)
    if selected_warehouse_id == BADER_WAREHOUSE_ID:
        try:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'write', [[presupuesto_id], {
                'carrier_id': BADER_CARRIER_ID
            }])
            logger.info(f"✅ Transportadora LOG Dentaltix (ID {BADER_CARRIER_ID}) definida no pedido {presupuesto_id}")
        except Exception as e:
            logger.error(f"❌ Erro ao definir transportadora no pedido: {e}")

    # ✅ Confirmar pedido no Odoo
    try:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'action_confirm', [[presupuesto_id]])
        flash('Presupuesto transformado en pedido con éxito!', 'success')
    except Exception as e:
        logger.error(f"Erro ao confirmar pedido: {e}")
        flash(f'Error al transformar el presupuesto en pedido: {e}', 'error')


    # ✅ Criar atividade no albarán para eva@bader.es quando armazém = Bader
    if selected_warehouse_id == BADER_WAREHOUSE_ID:
        try:
            # Buscar o albarán (stock.picking) associado ao pedido
            picking_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking', 'search',
                [[('origin', '=', presupuesto_data.get('name', '')),
                  ('picking_type_code', '=', 'outgoing')]], {'limit': 1})

            if picking_ids:
                picking_id = picking_ids[0]

                # Definir transportadora LOG Dentaltix no albarán (fallback)
                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking', 'write', [[picking_id], {
                    'carrier_id': BADER_CARRIER_ID
                }])
                logger.info(f"✅ Transportadora (ID {BADER_CARRIER_ID}) definida no albarán {picking_id}")

                # Buscar user_id de eva@bader.es
                eva_user_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.users', 'search',
                    [[('login', '=', ACTIVITY_PICKING_USER_EMAIL)]], {'limit': 1})
                eva_user_id = eva_user_ids[0] if eva_user_ids else uid

                # Buscar tipo de atividade "To Do"
                activity_type = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mail.activity.type', 'search',
                    [[('name', 'ilike', 'To Do')]], {'limit': 1})
                activity_type_id = activity_type[0] if activity_type else 4

                # Buscar model ID de stock.picking
                model_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'ir.model', 'search',
                    [[('model', '=', 'stock.picking')]], {'limit': 1})[0]

                deadline = datetime.now().strftime('%Y-%m-%d')
                order_name = presupuesto_data.get('name', str(presupuesto_id))
                partner_name = presupuesto_data.get('partner_id', ['', 'Cliente'])[1]

                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mail.activity', 'create', [{
                    'res_model_id': model_id,
                    'res_id': picking_id,
                    'activity_type_id': activity_type_id,
                    'summary': f'Preparar pedido {order_name}',
                    'note': f'<p>Pedido <b>{order_name}</b> de <b>{partner_name}</b> — Almacén Bader. Por favor preparar el albarán.</p>',
                    'user_id': eva_user_id,
                    'date_deadline': deadline,
                }])
                logger.info(f"✅ Atividade criada no albarán {picking_id} para {ACTIVITY_PICKING_USER_EMAIL}")
            else:
                logger.warning(f"⚠️ Nenhum albarán encontrado para o pedido {presupuesto_data.get('name', '')}")
        except Exception as e:
            logger.error(f"❌ Erro ao criar atividade no albarán: {e}")

    return redirect(url_for('payment', presupuesto_id=presupuesto_id))


@app.route('/send_email/<int:presupuesto_id>', methods=['POST'])
def send_email(presupuesto_id):
    # ✅ XSS Protection: sanitize the note input
    raw_note = request.form.get('note', '')
    note = sanitize_string(raw_note)
    payment_type = request.form.get('paymentType', '')
    payment_term_id = PAYMENT_TERM_CASH_ID if payment_type == 'cash' else PAYMENT_TERM_CARD_ID

    logger.info(f"Notas recebidas: {note}")
    logger.info(f"Tipo de pagamento: {payment_type}")

    # Autenticação com o Odoo
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return jsonify({'error': 'Falha na autenticação com o Odoo.'}), 401

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

    if note:
        # ✅ XSS Fix: note is already sanitized via escape()
        formatted_note = f"<div style='color: red; font-size: 40px;'>{note} 👍</div>"
        try:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'message_post', [presupuesto_id], {
                'body': formatted_note,
                'message_type': 'comment',
                'subtype_xmlid': 'mail.mt_note',
            })
        except Exception as e:
            logger.error(f'Erro ao postar mensagem no Odoo: {e}')
            return jsonify({'error': 'Não foi possível postar a mensagem no Odoo.'}), 500

        # ✅ Postar a mesma nota no albarán (stock.picking) associado
        try:
            picking_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking', 'search', [[('sale_id', '=', presupuesto_id)]])
            for picking_id in picking_ids:
                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.picking', 'message_post', [picking_id], {
                    'body': formatted_note,
                    'message_type': 'comment',
                    'subtype_xmlid': 'mail.mt_note',
                })
            if picking_ids:
                logger.info(f"✅ Nota postada no(s) albarán(es): {picking_ids}")
        except Exception as e:
            logger.error(f"Erro ao postar nota no albarán: {e}")

    try:
        sale_order_values = {'note': note, 'payment_term_id': payment_term_id}
        result = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'write', [presupuesto_id, sale_order_values])
        logger.info(f"Resultado da escrita no Odoo: {result}")
    except xmlrpc.client.Fault as fault:
        logger.error(f"Erro do Odoo: {fault}")
        return jsonify({'error': 'Não foi possível salvar as alterações no Odoo.'}), 500

    # Tente enviar o email após gravar a nota
    try:
        email_template_id = EMAIL_TEMPLATE_ID
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mail.template', 'send_mail', [email_template_id, presupuesto_id])
        flash('E-mail enviado com sucesso!', 'success')
        return jsonify({'success': 'Notas salvas e e-mail enviado com sucesso.'})
    except Exception as e:
        logger.error(f"Erro ao enviar email: {e}")
        return jsonify({'error': 'Erro ao enviar o e-mail.'}), 500


@app.route('/send_email_presupuesto/<int:presupuesto_id>', methods=['POST'])
def send_email_presupuesto(presupuesto_id):
    # Autenticação com o Odoo
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return redirect(url_for('index'))

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

    try:
        email_template_id = EMAIL_TEMPLATE_ID
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'mail.template', 'send_mail', [email_template_id, presupuesto_id])
        flash('E-mail enviado con éxito!', 'success')
    except Exception as e:
        logger.error(f"Erro ao enviar email do presupuesto: {e}")
        flash(str(e), 'error')

    return redirect(url_for('presupuesto_details', presupuesto_id=presupuesto_id))


@app.route('/cancel_presupuesto/<int:presupuesto_id>', methods=['POST'])
def cancel_presupuesto(presupuesto_id):
    cancel_note = sanitize_string(request.form.get('cancel_note', ''))

    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return redirect(url_for('index'))

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

    # Postar a nota de cancelamento no chatter do presupuesto
    if cancel_note:
        formatted_note = f"<div style='color: red; font-size: 20px;'><b>❌ CANCELADO:</b> {cancel_note}</div>"
        try:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'message_post', [presupuesto_id], {
                'body': formatted_note,
                'message_type': 'comment',
                'subtype_xmlid': 'mail.mt_note',
            })
            logger.info(f"✅ Nota de cancelamento postada no presupuesto {presupuesto_id}")
        except Exception as e:
            logger.error(f"Erro ao postar nota de cancelamento: {e}")

    # Cancelar o presupuesto no Odoo
    try:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'action_cancel', [[presupuesto_id]])
        flash('Presupuesto cancelado con éxito.', 'success')
        logger.info(f"✅ Presupuesto {presupuesto_id} cancelado")
    except Exception as e:
        logger.error(f"Erro ao cancelar presupuesto: {e}")
        flash(f'Error al cancelar: {str(e)}', 'error')

    return redirect(url_for('index'))


# ✅ SECURITY FIX: Changed from GET to POST to prevent CSRF attacks
@app.route('/delete_product/<int:line_id>', methods=['POST'])
def delete_product(line_id):
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return redirect(url_for('index'))

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

    # Tente obter o ID do orçamento primeiro
    try:
        presupuesto_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'read', [line_id], {'fields': ['order_id']})[0]['order_id'][0]
    except Exception as e:
        logger.error(f"Erro ao obter detalhes do produto para exclusão: {e}")
        flash('Error al obtener los detalles del producto. Quizás el producto no existe.', 'error')
        return redirect(url_for('index'))

    # Tente excluir a linha do produto
    try:
        result = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'unlink', [[line_id]])
        if result:
            flash('Producto eliminado con éxito!', 'success')
        else:
            flash('Error al eliminar el producto.', 'error')
    except Exception as e:
        logger.error(f"Erro ao excluir produto {line_id}: {e}")
        flash('Error al eliminar el producto.', 'error')

    return redirect(url_for('presupuesto_details', presupuesto_id=presupuesto_id))


@app.route('/payment/<int:presupuesto_id>', methods=['GET'])
def payment(presupuesto_id):
    """Exibe os detalhes de pagamento do pedido."""
    uid = odoo_authenticate()
    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return redirect(url_for('index'))

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    presupuesto_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', [presupuesto_id], {'context': {'lang': 'es_ES'}})

    if not presupuesto_data:
        flash('Error al buscar el presupuesto.', 'error')
        return redirect(url_for('index'))

    return render_template('payment.html', presupuesto_data=presupuesto_data[0])


# ===================================================
# Run the App
# ===================================================
if __name__ == '__main__':
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")

    # SSL is optional — only enabled if cert paths exist in .env
    ssl_ctx = (SSL_CERT, SSL_KEY) if SSL_CERT and SSL_KEY else None

    app.run(
        host='0.0.0.0',
        port=500,
        debug=debug_mode,
        ssl_context=ssl_ctx
    )