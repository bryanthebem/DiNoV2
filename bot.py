# bot.py (Versão final com servidor Flask e roteamento inteligente)
# bot.py (Versão final com servidor Flask e roteamento inteligente)

import discord
from discord import app_commands, Interaction, SelectOption, Color
from discord.ext import commands
from discord.ui import Select, View
import os
from dotenv import load_dotenv
from typing import Optional
import threading
from flask import Flask, request, jsonify

# Módulos locais
from notion_integration import NotionIntegration, NotionAPIError
from config_utils import save_config, load_config
from ui_components import * # Importa tudo de ui_components

# Carregar variáveis de ambiente e inicializar
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# --- CONFIGURAÇÃO DO BOT E NOTION ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True # Necessário para encontrar membros por nome

bot = commands.Bot(command_prefix="!", intents=intents)
notion = NotionIntegration()

# --- CONFIGURAÇÃO DO SERVIDOR FLASK ---
app = Flask(__name__)

@app.route('/webhook/notion', methods=['POST'])
def notion_webhook_handler():
    secret_from_url = request.args.get('secret')
    if not WEBHOOK_SECRET or not secret_from_url or secret_from_url != WEBHOOK_SECRET:
        print("ALERTA: Tentativa de acesso ao webhook sem o segredo correto.")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    data = request.json
    bot.loop.call_soon_threadsafe(bot.dispatch, 'notion_event', data)
    return jsonify({"status": "success"}), 200

# --- EVENTOS DO BOT ---

@bot.event
async def on_ready():
    await bot.wait_until_ready()
    if DISCORD_GUILD_ID:
        guild = discord.Object(id=DISCORD_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Comandos sincronizados para o servidor {DISCORD_GUILD_ID}.")
    else:
        await bot.tree.sync()
        print("Comandos sincronizados globalmente.")
    print(f"✅ {bot.user} está online e pronto para uso!")
    print(f"🚀 Servidor de webhook está a ouvir...")

@bot.event
async def on_notion_event(page_data: dict):
    try:
        page_id = page_data.get('id')
        if not page_id: return

        # Para descobrir a qual configuração este webhook pertence, teríamos que
        # ter um identificador na URL. Para manter simples, vamos assumir que
        # a configuração é a nível de servidor e vamos pegar a primeira que encontrarmos.
        server_config = load_config(DISCORD_GUILD_ID)
        if not server_config or 'channels' not in server_config: return

        # Encontra a configuração do canal que tem as preferências de notificação
        channel_config = next((c for c in server_config['channels'].values() if 'notification_preference' in c), None)
        if not channel_config: return
        
        guild = bot.get_guild(int(DISCORD_GUILD_ID))
        if not guild: return

        preference = channel_config.get('notification_preference')
        if preference == 'disabled':
            print(f"Notificação para página {page_id} ignorada (desativado).")
            return

        page_result = {'id': page_id, 'url': page_data.get('url'), 'properties': page_data.get('properties', {})}
        display_props = channel_config.get('display_properties', [])
        embed = notion.format_page_for_embed(page_result, display_properties=display_props)
        if not embed: return

        # --- LÓGICA DE ROTEAMENTO BASEADA NA PREFERÊNCIA ---
        if preference == 'topic':
            topic_prop_name = channel_config.get('topic_link_property_name')
            topic_url = notion.extract_value_from_property(page_result['properties'].get(topic_prop_name, {}), 'url')
            if topic_url:
                topic_id = int(topic_url.split('/')[-1])
                target_topic = bot.get_channel(topic_id)
                if isinstance(target_topic, discord.Thread):
                    embed.title = f"🔔 Atualização no Tópico: {embed.title.replace('📌 ', '')}"
                    await target_topic.send(embed=embed)
                    return

        elif preference == 'dm':
            dm_prop_name = channel_config.get('dm_notification_prop')
            user_name = notion.extract_value_from_property(page_result['properties'].get(dm_prop_name, {}), 'people')
            if user_name:
                target_user = discord.utils.get(guild.members, display_name=user_name)
                if target_user:
                    embed.title = f"🔔 Você recebeu uma atualização: {embed.title.replace('📌 ', '')}"
                    await target_user.send(embed=embed)
                    return

        # Fallback para o canal configurado se 'topic' ou 'dm' falharem, ou se a preferência for 'channel'
        target_channel_id = channel_config.get('notification_target_id')
        if target_channel_id:
            target_channel = bot.get_channel(int(target_channel_id))
            if target_channel:
                embed.title = f"🔔 Atualização do Notion: {embed.title.replace('📌 ', '')}"
                await target_channel.send(embed=embed)

    except Exception as e:
        print(f"Erro ao processar o evento do Notion: {e}")

# Adicione esta classe e a função abaixo ao seu arquivo bot.py
# antes da definição de @bot.tree.command(name="config", ...)

class PropertySelectionView(discord.ui.View):
    """Uma View para permitir que o admin selecione as propriedades de criação e exibição."""
    def __init__(self, author_id: int, properties: list, notion_url: str, guild_id: int, channel_id: int, is_update: bool):
        super().__init__(timeout=300.0)
        self.author_id = author_id
        self.notion_url = notion_url
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.is_update = is_update
        self.create_properties = []
        self.display_properties = []

        property_options = [discord.SelectOption(label=p['name'], description=f"Tipo: {p['type']}") for p in properties[:25]]

        # Menu para selecionar propriedades de CRIAÇÃO
        self.create_select = discord.ui.Select(
            placeholder="Selecione as propriedades para CRIAR cards...",
            min_values=1,
            max_values=len(property_options),
            options=property_options,
            custom_id="create_select"
        )
        self.add_item(self.create_select)

        # Menu para selecionar propriedades de EXIBIÇÃO/BUSCA
        self.display_select = discord.ui.Select(
            placeholder="Selecione as propriedades para EXIBIR/BUSCAR...",
            min_values=1,
            max_values=len(property_options),
            options=property_options,
            custom_id="display_select"
        )
        self.add_item(self.display_select)

        # Botão para salvar
        self.save_button = discord.ui.Button(label="Salvar Configuração", style=discord.ButtonStyle.green, row=2)
        self.add_item(self.save_button)

        # Callbacks
        self.create_select.callback = self.select_callback
        self.display_select.callback = self.select_callback
        self.save_button.callback = self.save_callback

    async def select_callback(self, interaction: discord.Interaction):
        # Apenas confirma a interação para o Discord saber que foi recebida
        await interaction.response.defer()
        if interaction.data["custom_id"] == "create_select":
            self.create_properties = interaction.data["values"]
        elif interaction.data["custom_id"] == "display_select":
            self.display_properties = interaction.data["values"]

    async def save_callback(self, interaction: discord.Interaction):
        if not self.create_properties or not self.display_properties:
            await interaction.response.send_message("❌ Você precisa selecionar propriedades para ambas as listas.", ephemeral=True)
            return

        # Salva a configuração no arquivo JSON
        new_config = {
            "notion_url": self.notion_url,
            "create_properties": self.create_properties,
            "display_properties": self.display_properties
        }
        save_config(self.guild_id, self.channel_id, new_config)

        status = "atualizada" if self.is_update else "concluída"
        await interaction.response.edit_message(content=f"✅ Configuração {status} com sucesso! Você já pode usar os comandos `/card` e `/busca`.", view=None)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Você não pode interagir com o menu de outra pessoa.", ephemeral=True)
            return False
        return True


async def run_full_config_flow(interaction: Interaction, url: str, is_update: bool):
    """Inicia o fluxo de configuração completo, pedindo ao usuário para selecionar as propriedades."""
    channel_id = interaction.channel.parent_id if isinstance(interaction.channel, discord.Thread) else interaction.channel.id
    try:
        properties = notion.get_properties_for_interaction(url)
        if not properties:
            await interaction.followup.send("❌ Não foi possível obter as propriedades da base de dados. Verifique a URL e as permissões do bot no Notion.", ephemeral=True)
            return

        view = PropertySelectionView(
            author_id=interaction.user.id,
            properties=properties,
            notion_url=url,
            guild_id=interaction.guild_id,
            channel_id=channel_id,
            is_update=is_update
        )
        message = (
            "**Passo 1:** Selecione na primeira lista as propriedades que devem aparecer no formulário de criação (`/card`).\n"
            "**Passo 2:** Selecione na segunda lista as propriedades que serão exibidas nos cards e usadas na busca (`/busca`).\n\n"
            "*(Dica: você pode selecionar as mesmas propriedades para ambos)*"
        )
        await interaction.followup.send(message, view=view, ephemeral=True)

    except NotionAPIError as e:
        await interaction.followup.send(f"❌ Erro ao acessar o Notion: {e}", ephemeral=True)
    except Exception as e:
        print(f"Erro inesperado no fluxo de configuração: {e}")
        await interaction.followup.send(f"🔴 Um erro inesperado ocorreu durante a configuração: {e}", ephemeral=True)

# --- COMANDOS DE BARRA (/) ---

@bot.tree.command(name="config", description="(Admin) Configura ou gerencia o bot para este canal.")
@app_commands.describe(url="Opcional: URL da base de dados do Notion para configurar ou reconfigurar.")
@app_commands.checks.has_permissions(administrator=True)
async def config_command(interaction: Interaction, url: Optional[str] = None):
    await interaction.response.defer(ephemeral=True, thinking=True)

    channel_id = interaction.channel.parent_id if isinstance(interaction.channel, discord.Thread) else interaction.channel.id
    config = load_config(interaction.guild_id, channel_id)

    if url:
        if not notion.extract_database_id(url):
            return await interaction.followup.send("❌ A URL do Notion fornecida parece ser inválida. Verifique se é a URL de uma base de dados.", ephemeral=True)

        await interaction.followup.send("Iniciando a configuração/reconfiguração completa...", ephemeral=True)
        await run_full_config_flow(interaction, url, is_update=bool(config))
        return

    if config and 'notion_url' in config:
        view = ManagementView(interaction, notion, config)
        await interaction.followup.send("Este canal já está configurado. Escolha uma opção de gerenciamento:", view=view, ephemeral=True)
    else:
        await interaction.followup.send("❌ Este canal ainda não foi configurado. Use `/config` e forneça a URL da sua base de dados do Notion.", ephemeral=True)

@config_command.error
async def config_command_error(interaction: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ Você precisa ser um administrador para usar este comando."
    else:
        message = f"🔴 Um erro de comando ocorreu: {error}"
        print(f"Erro no comando /config: {error}")

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="card", description="Abre um formulário para criar um novo card no Notion.")
async def interactive_card(interaction: Interaction):
    try:
        config_channel_id = interaction.channel.parent_id if isinstance(interaction.channel, discord.Thread) else interaction.channel.id
        config = load_config(interaction.guild_id, config_channel_id)

        if not config or 'notion_url' not in config:
            return await interaction.response.send_message("❌ O Notion ainda não foi configurado para este canal. Peça para um admin usar `/config`.", ephemeral=True)

        all_properties = notion.get_properties_for_interaction(config['notion_url'])

        thread_context = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        topic_title = thread_context.name if thread_context else None

        create_properties_names = config.get('create_properties', []).copy()

        # Remove propriedades que são preenchidas automaticamente
        props_to_remove = [
            config.get('topic_link_property_name'),
            config.get('individual_person_prop'),
            config.get('collective_person_prop')
        ]
        create_properties_names = [p for p in create_properties_names if p and p not in props_to_remove]

        if not create_properties_names:
            return await interaction.response.send_message("❌ Nenhuma propriedade foi configurada para criação manual de cards. Use `/config` para ajustar.", ephemeral=True)

        properties_to_ask = [prop for prop in all_properties if prop['name'] in create_properties_names]
        text_props = [p for p in properties_to_ask if p['type'] not in ['select', 'multi_select', 'status']]
        select_props = [p for p in properties_to_ask if p['type'] in ['select', 'multi_select', 'status']]

        # Validação da quantidade de campos
        if len(text_props) > 5: return await interaction.response.send_message(f"❌ Formulário com muitos campos de texto ({len(text_props)}). O máximo é 5.", ephemeral=True)
        if len(select_props) > 4: return await interaction.response.send_message(f"❌ Formulário com muitos menus de seleção ({len(select_props)}). O máximo é 4.", ephemeral=True)

        modal = CardModal(
            notion=notion,
            config=config,
            all_properties=all_properties,
            text_props=text_props,
            select_props=select_props,
            thread_context=thread_context,
            topic_title=topic_title
        )
        await interaction.response.send_modal(modal)

    except Exception as e:
        error_message = f"🔴 Erro inesperado ao iniciar o comando `/card`: {e}"
        print(error_message)
        if not interaction.response.is_done():
            await interaction.response.send_message(error_message, ephemeral=True)


@bot.tree.command(name="busca", description="Busca ou edita um card no Notion.")
async def interactive_search(interaction: Interaction):
    try:
        config_channel_id = interaction.channel.parent_id if isinstance(interaction.channel, discord.Thread) else interaction.channel.id
        config = load_config(interaction.guild_id, config_channel_id)
        if not config or 'notion_url' not in config:
            return await interaction.response.send_message("❌ O Notion não foi configurado para este canal. Use `/config`.", ephemeral=True)

        all_properties = notion.get_properties_for_interaction(config['notion_url'])
        display_properties_names = config.get('display_properties', [])
        if not display_properties_names:
            return await interaction.response.send_message("❌ As propriedades para busca não foram configuradas. Use `/config`.", ephemeral=True)

        searchable_options = [prop for prop in all_properties if prop['name'] in display_properties_names]
        if not searchable_options:
            return await interaction.response.send_message("❌ Nenhuma propriedade pesquisável configurada.", ephemeral=True)

        class PropertySelect(Select):
            def __init__(self, searchable_props, author_id):
                self.searchable_props = searchable_props
                self.author_id = author_id
                opts = [SelectOption(label=p['name'], description=f"Tipo: {p['type']}") for p in self.searchable_props[:25]]
                super().__init__(placeholder="Escolha uma propriedade para pesquisar...", options=opts)

            async def callback(self, inter: Interaction):
                if inter.user.id != self.author_id:
                    return await inter.response.send_message("Você não pode interagir com o menu de outra pessoa.", ephemeral=True)

                selected_prop_name = self.values[0]
                selected_property = next((p for p in all_properties if p['name'] == selected_prop_name), None)

                if selected_property['type'] in ['select', 'multi_select', 'status']:
                    prop_options = selected_property.get('options', [])

                    class OptionSelect(Select):
                        def __init__(self):
                            opts = [SelectOption(label=opt) for opt in prop_options[:25]]
                            super().__init__(placeholder=f"Escolha uma opção de '{selected_property['name']}'...", options=opts)

                        async def callback(self, sub_inter: Interaction):
                            await sub_inter.response.defer(thinking=True, ephemeral=True)
                            search_term = self.values[0]
                            cards = notion.search_in_database(config['notion_url'], search_term, selected_property['name'], selected_property['type'])
                            results = cards.get('results', [])
                            if not results:
                                return await sub_inter.followup.send(f"❌ Nenhum resultado para '{search_term}'.", ephemeral=True)

                            await sub_inter.followup.send(f"✅ {len(results)} resultado(s) encontrado(s)!", ephemeral=True)

                            view = PaginationView(sub_inter.user, results, config, notion, actions=['edit', 'delete', 'share'])
                            view.update_nav_buttons()
                            await sub_inter.followup.send(embed=await view.get_page_embed(), view=view, ephemeral=True)

                    view_options = View(timeout=120.0)
                    view_options.add_item(OptionSelect())
                    await inter.response.edit_message(content=f"➡️ Escolha um valor para **{selected_property['name']}**:", view=view_options)
                else:
                    await inter.response.send_modal(SearchModal(notion=notion, config=config, selected_property=selected_property))

        initial_view = View(timeout=180.0)
        initial_view.add_item(PropertySelect(searchable_options, interaction.user.id))
        await interaction.response.send_message("🔎 Escolha no menu abaixo a propriedade para sua busca.", view=initial_view, ephemeral=True)

    except NotionAPIError as e:
        msg = f"❌ Erro com o Notion: {e}"
        if not interaction.response.is_done(): await interaction.response.send_message(msg, ephemeral=True)
        else: await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        msg = f"🔴 Erro inesperado: {e}"
        if not interaction.response.is_done(): await interaction.response.send_message(msg, ephemeral=True)
        else: await interaction.followup.send(msg, ephemeral=True)
        print(f"Erro inesperado no /busca: {e}")


@bot.tree.command(name="num_cards", description="Mostra o total de cards no banco de dados do canal.")
async def num_cards(interaction: Interaction):
    try:
        config_channel_id = interaction.channel.parent_id if isinstance(interaction.channel, discord.Thread) else interaction.channel.id
        config = load_config(interaction.guild_id, config_channel_id)
        if not config or 'notion_url' not in config:
            return await interaction.response.send_message("❌ O Notion não foi configurado para este canal. Use `/config`.", ephemeral=True)
        count = notion.get_database_count(config['notion_url'])
        await interaction.response.send_message(f"📊 O banco de dados deste canal contém **{count}** cards.")
    except NotionAPIError as e:
        await interaction.response.send_message(f"❌ Erro ao acessar o Notion: {e}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"🔴 Erro inesperado: {e}", ephemeral=True)
        print(f"Erro inesperado no /num_cards: {e}")


# --- FUNÇÃO PARA INICIAR O SERVIDOR FLASK ---
def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


# --- INICIAR O BOT ---
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    if DISCORD_TOKEN:
        try:
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            print(f"❌ Erro fatal ao iniciar o bot: {e}")
    else:
        print("❌ Token do Discord (DISCORD_TOKEN) não encontrado no arquivo .env")
