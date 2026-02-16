import os
import json
import re
import logging
import xmlrpc.client

from flask import (
    Flask, request, jsonify,
    redirect, url_for, flash, render_template, session
)
from flask_wtf.csrf import CSRFProtect
from markupsafe import escape
from dotenv import load_dotenv
from zeep import Client
import requests

# ===================================================
# Load environment variables from .env
# ===================================================
load_dotenv()

# ===================================================
# Flask App Configuration
# ===================================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32).hex())

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
MANDATORY_TAG_IDS = [int(x) for x in os.getenv("MANDATORY_TAG_IDS", "319,403").split(",")]

# Warehouse & Payment
WAREHOUSE_ID = int(os.getenv("WAREHOUSE_ID", "19"))
PAYMENT_TERM_CASH_ID = int(os.getenv("PAYMENT_TERM_CASH_ID", "33"))
PAYMENT_TERM_CARD_ID = int(os.getenv("PAYMENT_TERM_CARD_ID", "34"))
EMAIL_TEMPLATE_ID = int(os.getenv("EMAIL_TEMPLATE_ID", "162"))

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

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

    domain = ['|', ('vat', 'ilike', query), ('email', 'ilike', query)]
    partner_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                   'res.partner', 'search', [domain])
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
        "vat": query
    }

    if partner_ids:
        partner = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                    'res.partner', 'read', [partner_ids])[0]

        state_name = partner.get('state_id', ["", ""])[1].split(' (')[0] if isinstance(partner.get('state_id'), list) else ""
        country_name = partner.get('country_id', ["", ""])[1] if isinstance(partner.get('country_id'), list) else ""
        partner_data["vat"] = partner.get("vat", "")
        partner_data["zip"] = partner.get("zip", "")
        session['partner_id'] = partner_ids[0]

        partner_data = {
            "name": partner.get("name", ""),
            "street": partner.get('street', ""),
            "city": partner.get('city', ""),
            "zip": partner.get("zip", ""),
            "state": state_name,
            "country": country_name,
            "phone": partner.get("phone", ""),
            "mobile": partner.get("mobile", ""),
            "email": partner.get("email", ""),
            "vat": partner.get("vat")
        }
    else:
        # Se não encontrar, tenta buscar na API VIES
        if len(query) > 2 and not partner_ids:
            country_code = query[:2].upper()
            vat_number = query[2:]
            client = Client('http://ec.europa.eu/taxation_customs/vies/checkVatService.wsdl')
            logger.info(f"Iniciando a busca pelo VAT: {query}")
            try:
                response = client.service.checkVat(country_code, vat_number)

                if response.valid:
                    address_parts = response.address.split('\n')
                    street_and_number = address_parts[0].strip() if len(address_parts) > 0 else ""
                    city_and_zip = address_parts[1].strip() if len(address_parts) > 1 else ""
                    city = city_and_zip.split('\n')[0].strip() if city_and_zip else ""
                    city_zip_part = address_parts[-1].strip() if len(address_parts) > 2 else ""
                    zip_code = city_zip_part.split(' ')[0] if city_zip_part else ""
                    full_country_name = ""
                    if country_code == "PT":
                        full_country_name = "Portugal"
                    elif country_code == "ES":
                        full_country_name = "Spain"
                    elif country_code == "IT":
                        full_country_name = "Italy"
                    elif country_code == "FR":
                        full_country_name = "France"

                    partner_data.update({
                        "name": response.name.strip(),
                        "street": street_and_number,
                        "city": city,
                        "zip": zip_code,
                        "country": full_country_name,
                        "state": city,
                        "vat": f"{country_code}{vat_number}"
                    })
                    logger.info(f"Dados carregados da API VIES: {partner_data}")
                    flash('Datos cargados desde la API VIES.', 'success')
                else:
                    logger.warning("VAT não válido ou não encontrado na API VIES.")
                    flash('VAT no válido o no encontrado en la API VIES.', 'warning')
            except Exception as e:
                logger.error(f"Erro ao consultar a API VIES: {e}")
                flash(f'Error al consultar la API VIES: {str(e)}', 'error')

    # ✅ buscar todos os países da base do Odoo
    countries = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.country', 'search_read', [[], ['name']], {'limit': 999})

    return render_template('search.html', data=partner_data, countries=countries)


@app.route('/save', methods=['POST'])
def save():
    # Coletar os dados do formulário
    name = request.form.get('name', '').strip()
    vat = request.form.get('vat', '').strip()
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

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

    # Buscar IDs do estado e do país no Odoo
    state_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.country.state', 'search', [[('name', '=', state_name)]], {'limit': 1})
    country_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.country', 'search', [[('name', '=', country_name)]], {'limit': 1})

    # Mapeamento dos IDs das etiquetas (configurados via .env)
    tag_mapping = {
        "clinica_dental": TAG_CLINICA_DENTAL_ID,
        "laboratorio": TAG_LABORATORIO_ID,
        "estudiante": TAG_ESTUDIANTE_ID,
        "mayorista": TAG_MAYORISTA_ID,
        "otros": TAG_OTROS_ID
    }

    # Obtém o ID correspondente à etiqueta selecionada, caso contrário, usa "Otros"
    tag_id = tag_mapping.get(customer_tag, TAG_OTROS_ID)

    # IDs fixos que devem ser adicionados a todos os clientes
    mandatory_tags = MANDATORY_TAG_IDS

    # Lista final de etiquetas a serem associadas ao cliente
    tag_ids = [tag_id] + mandatory_tags

    # Definir a tarifa (pricelist_id) com base na etiqueta selecionada
    pricelist_id = PRICELIST_MAYORISTA_ID if tag_id == TAG_MAYORISTA_ID else PRICELIST_DEFAULT_ID

    # Verificar se o cliente já existe no Odoo
    partner_domain = ['|', ('vat', '=', vat), ('email', '=', email)]
    existing_partner_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search', [partner_domain])

    if existing_partner_ids:
        # Atualizar cliente existente
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'write', [existing_partner_ids, {
            'name': name,
            'vat': vat if vat and len(vat) >= 4 and vat[:2].isalpha() else False,
            'email': email,
            'street': street,
            'city': city,
            'state_id': state_id[0] if state_id else False,
            'country_id': country_id[0] if country_id else False,
            'phone': phone,
            'mobile': mobile,
            'zip': zip_code,
            'category_id': [(6, 0, tag_ids)],
            'property_product_pricelist': pricelist_id
        }])
        session['partner_id'] = existing_partner_ids[0]
        flash('Datos del cliente actualizados con éxito!', 'success')
    else:
        # Criar novo cliente no Odoo
        new_partner_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'create', [{
            'name': name,
            'vat': vat if vat and len(vat) >= 4 and vat[:2].isalpha() else False,
            'email': email,
            'street': street,
            'city': city,
            'state_id': state_id[0] if state_id else False,
            'country_id': country_id[0] if country_id else False,
            'phone': phone,
            'mobile': mobile,
            'zip': zip_code,
            'category_id': [(6, 0, tag_ids)],
            'property_product_pricelist': pricelist_id
        }])

        if new_partner_id:
            session['partner_id'] = new_partner_id
            return redirect(url_for('create_presupuesto', vat=vat))
        else:
            flash('Error al crear el cliente.', 'error')

    return redirect(url_for('create_presupuesto', vat=vat))


@app.route('/create_presupuesto', methods=['GET'])
def create_presupuesto():
    vat = request.args.get('vat')

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

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

    presupuesto_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'create', [{
        'partner_id': session.get('partner_id'),
        'pricelist_id': PRICELIST_ORDER_ID,
        'fiscal_position_id': fiscal_position_id,
        'client_order_ref': "Expodental 2026",
        'team_id': SALES_TEAM_ID
    }])

    if presupuesto_id:
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
                with open('price_list_feira.json', 'r') as file:
                    custom_prices = json.load(file)
                    custom_prices_lower = {k.lower(): v for k, v in custom_prices.items()}
            except FileNotFoundError:
                custom_prices_lower = {}

            custom_price = custom_prices_lower.get(product_code)

            # Busque o produto pelo seu código de referência
            product_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search', [[['default_code', 'ilike', product_code]]])

            if product_ids:
                product_id = product_ids[0]

                sale_order_line_vals = {
                    'order_id': presupuesto_id,
                    'product_id': product_id,
                    'product_uom_qty': product_qty
                }

                if custom_price is not None:
                    sale_order_line_vals['price_unit'] = custom_price

                new_line_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'create', [sale_order_line_vals])

                if new_line_id:
                    flash('Producto añadido con éxito!', 'success')
                else:
                    flash('Error al añadir el producto.', 'error')
            else:
                flash('Producto no encontrado.', 'error')

    # Pegue os detalhes do Presupuesto pelo ID
    presupuesto_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', [presupuesto_id])[0]

    sale_order_lines = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.line', 'search_read', [[['order_id', '=', presupuesto_id]]], {'fields': ['product_id', 'name', 'product_uom_qty', 'price_total']})

    return render_template('presupuesto.html', presupuesto_data=presupuesto_data, presupuesto_id=presupuesto_id, sale_order_lines=sale_order_lines)


@app.route('/confirm_presupuesto/<int:presupuesto_id>', methods=['POST'])
def confirm_presupuesto(presupuesto_id):
    """Confirma um orçamento no Odoo e atualiza o campo 'Valor Pedido' no Clientify."""
    uid = odoo_authenticate()
    if not uid:
        flash('Falha na autenticação com o Odoo.', 'error')
        return redirect(url_for('index'))

    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    presupuesto_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', [presupuesto_id])

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

    # ✅ Atualizar armazém
    try:
        update_result = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'write', [[presupuesto_id], {
            'warehouse_id': WAREHOUSE_ID
        }])
        if update_result:
            logger.info(f"✅ Armazém atualizado para ID 19 no pedido {presupuesto_id}")
    except Exception as e:
        logger.error(f"❌ Falha ao modificar armazém: {e}")

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

    # ✅ Confirmar pedido no Odoo
    try:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'action_confirm', [[presupuesto_id]])
        flash('Presupuesto transformado en pedido con éxito!', 'success')
    except Exception as e:
        logger.error(f"Erro ao confirmar pedido: {e}")
        flash(f'Error al transformar el presupuesto en pedido: {e}', 'error')

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
    presupuesto_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', [presupuesto_id])

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