# ui_components.py (Versão com correção do custom_id)

import discord
from discord import Interaction, SelectOption, ButtonStyle, Color
from discord.ui import View, Button, Select, Modal, TextInput
import asyncio
from typing import List, Optional, Dict, Any
from datetime import datetime

# Módulos locais
from notion_integration import NotionIntegration, NotionAPIError
from config_utils import save_config, load_config
from ia_processor import summarize_thread_content

# --- FUNÇÕES AUXILIARES DE UI ---

async def get_first_message(thread: discord.Thread) -> Optional[discord.Message]:
    """
    Busca a primeira mensagem de um tópico (a que o iniciou).
    """
    try:
        # A propriedade starter_message é a forma mais confiável
        return await thread.fetch_message(thread.id)
    except (discord.NotFound, discord.Forbidden):
        # Fallback para o histórico se o starter_message falhar
        async for message in thread.history(limit=1, oldest_first=True):
            return message
    return None

async def get_topic_participants(thread: discord.Thread, limit: int = 100) -> set[discord.Member]:
    """Busca os participantes únicos de um tópico com base no histórico de mensagens."""
    participants = set()
    async for message in thread.history(limit=limit):
        if not message.author.bot:
            participants.add(message.author)
    return participants

async def get_thread_attachments(thread: discord.Thread, limit: int = 100) -> List[Dict[str, str]]:
    """
    Busca URLs de anexos de imagens, GIFs e vídeos em um tópico.
    Retorna uma lista de dicionários com 'type' e 'url'.
    """
    attachments_data = []
    async for message in thread.history(limit=limit):
        if message.attachments:
            for attachment in message.attachments:
                if attachment.content_type.startswith(('image/', 'video/')) or attachment.filename.lower().endswith(('.gif')):
                    attachments_data.append({
                        "type": attachment.content_type.split('/')[0],
                        "url": attachment.url,
                        "filename": attachment.filename
                    })
    return attachments_data


async def _build_notion_page_content(config: dict, thread_context: Optional[discord.Thread], notion_integration: NotionIntegration, command_name: str) -> Optional[List[Dict]]:
    """
    Constrói o corpo da página do Notion com base nas configurações ativadas para o comando específico.
    """
    page_content = []
    if not thread_context:
        return None

    # 1. Captura da Primeira Mensagem
    if command_name in config.get('capture_first_message_for_commands', []):
        first_message = await get_first_message(thread_context)
        if first_message and first_message.content:
            page_content.extend([
                {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "✉️ Mensagem Inicial"}}]}},
                {"object": "block", "type": "quote", "quote": {"rich_text": notion_integration._convert_text_to_notion_rich_text_objects(first_message.content)}},
                {"object": "block", "type": "divider", "divider": {}}
            ])

    # 2. Resumo da IA
    if command_name in config.get('ai_summary_for_commands', []):
        messages = [msg async for msg in thread_context.history(limit=100)]
        if messages:
            summary_text = await summarize_thread_content(messages)
            if summary_text and not summary_text.startswith("Erro:"):
                page_content.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🤖 Resumo da IA"}}]}})
                parsed_summary_blocks = notion_integration._parse_summary_to_notion_blocks(summary_text)
                page_content.extend(parsed_summary_blocks)

    # 3. Anexos
    attachments = await get_thread_attachments(thread_context)
    if attachments:
        if page_content:
            page_content.append({"object": "block", "type": "divider", "divider": {}})
        page_content.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "📎 Anexos do Tópico"}}]}})
        for att in attachments:
            block_type = 'image' if att['type'] == 'image' else 'paragraph'
            content = {"type": "external", "external": {"url": att['url']}} if block_type == 'image' else {"rich_text": [{"type": "text", "text": {"content": f"Vídeo/GIF ({att['filename']}): "}}, {"type": "text", "text": {"content": att['url'], "link": {"url": att['url']}}}]}
            page_content.append({"object": "block", "type": block_type, block_type: content})

    return page_content if page_content else None


async def start_editing_flow(interaction: Interaction, page_id_to_edit: str, config: dict, notion: NotionIntegration):
    try:
        all_db_props = notion.get_properties_for_interaction(config['notion_url'])
        editable_props = [p for p in all_db_props if p['name'] in config.get('create_properties', [])]

        prop_msg = await interaction.followup.send("Iniciando edição...", ephemeral=True)

        while True:
            prop_select_view = View(timeout=180.0)
            prop_select = Select(placeholder="Escolha uma propriedade para editar...", options=[SelectOption(label=p['name'], description=f"Tipo: {p['type']}") for p in editable_props[:25]])
            prop_select_view.add_item(prop_select)

            await prop_msg.edit(content="Qual propriedade você quer alterar agora?", view=prop_select_view)

            prop_choice_interaction = None
            async def prop_select_callback(inter: Interaction):
                nonlocal prop_choice_interaction
                prop_choice_interaction = inter
                prop_select_view.stop()
            prop_select.callback = prop_select_callback

            await prop_select_view.wait()

            if prop_choice_interaction is None:
                await prop_msg.edit(content="⌛ Edição cancelada ou tempo esgotado.", view=None)
                break

            selected_prop_name = prop_select.values[0]
            selected_prop_details = next((p for p in editable_props if p['name'] == selected_prop_name), None)

            new_value = None
            prop_type = selected_prop_details['type']

            if prop_type in ['select', 'multi_select', 'status']:
                options_view = View(timeout=180.0)
                options = selected_prop_details.get('options', [])
                options_select = Select(
                    placeholder=f"Escolha para {selected_prop_name}",
                    options=[SelectOption(label=opt) for opt in options[:25]],
                    max_values=len(options) if prop_type == 'multi_select' else 1
                )

                options_view.result = None
                async def options_select_callback(inter_opt: Interaction):
                    await inter_opt.response.defer()
                    options_view.result = inter_opt.data['values']
                    options_view.stop()

                options_select.callback = options_select_callback
                options_view.add_item(options_select)

                await prop_choice_interaction.response.edit_message(content=f"Qual o novo valor para **{selected_prop_name}**?", view=options_view)
                await options_view.wait()

                if options_view.result:
                    new_value = options_view.result if prop_type == 'multi_select' else options_view.result[0]

            else:
                class EditModal(Modal, title=f"Editar '{selected_prop_name}'"):
                    new_val_input = TextInput(label="Novo valor", style=discord.TextStyle.paragraph)
                    async def on_submit(self, modal_inter: Interaction):
                        self.result = self.new_val_input.value
                        await modal_inter.response.defer()
                        self.stop()

                edit_modal = EditModal()
                await prop_choice_interaction.response.send_modal(edit_modal)
                await edit_modal.wait()
                new_value = getattr(edit_modal, 'result', None)

            if new_value is None:
                await prop_msg.edit(content="❌ Nenhum novo valor fornecido.", view=None)
                await asyncio.sleep(5)
                continue

            await prop_msg.edit(content=f"⚙️ Atualizando propriedade...", view=None)
            properties_payload = notion.build_update_payload(selected_prop_name, prop_type, new_value)
            notion.update_page(page_id_to_edit, properties_payload)

            continue_view = ContinueEditingView(interaction.user.id)
            await prop_msg.edit(content=f"✅ Propriedade **{selected_prop_name}** atualizada!\nDeseja continuar editando?", view=continue_view)
            await continue_view.wait()

            if continue_view.choice == 'finish':
                await prop_msg.edit(content="Finalizando...", view=None)
                break

        final_page_data = notion.get_page(page_id_to_edit)
        display_names = config.get('display_properties', [])
        final_embed = notion.format_page_for_embed(final_page_data, display_properties=display_names)

        if final_embed:
            publish_view = PublishView(interaction.user.id, final_embed, page_id_to_edit, config, notion)
            await prop_msg.edit(content="Edição concluída! Veja o resultado.", embed=final_embed, view=publish_view)
        else:
            await prop_msg.edit(content="✅ Edição concluída!", embed=None, view=None)

    except Exception as e:
        print(f"Erro no fluxo de edição: {e}")
        try:
            msg_content = f"🔴 Um erro ocorreu durante a edição: {e}"
            if 'prop_msg' in locals() and prop_msg: await prop_msg.edit(content=msg_content, view=None, embed=None)
            else: await interaction.followup.send(msg_content, ephemeral=True)
        except: pass


# --- CLASSES DE UI ---

class SelectView(View):
    def __init__(self, select_component: Select, author_id: int, timeout=180.0):
        super().__init__(timeout=timeout)
        self.select_component, self.author_id = select_component, author_id
        self.add_item(self.select_component)
    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Você não pode interagir com o menu de outra pessoa.", ephemeral=True)
            return False
        return True

class CardActionView(View):
    def __init__(self, author_id: int, page_id: str, config: dict, notion: NotionIntegration):
        super().__init__(timeout=None)
        self.author_id, self.page_id, self.config, self.notion = author_id, page_id, config, notion

    @discord.ui.button(label="✏️ Editar", style=ButtonStyle.secondary)
    async def edit_button(self, interaction: Interaction, button: Button):
        await interaction.response.send_message("Iniciando modo de edição para este card...", ephemeral=True)
        await start_editing_flow(interaction, self.page_id, self.config, self.notion)

    @discord.ui.button(label="🗑️ Excluir", style=ButtonStyle.danger)
    async def delete_button(self, interaction: Interaction, button: Button):
        confirm_view = View(timeout=60.0)
        yes_button, no_button = Button(label="Sim, excluir!", style=ButtonStyle.danger), Button(label="Cancelar", style=ButtonStyle.secondary)
        confirm_view.add_item(yes_button); confirm_view.add_item(no_button)

        async def yes_callback(inter: Interaction):
            confirm_view.stop()
            try:
                await inter.response.defer(ephemeral=True, thinking=True)
                self.notion.delete_page(self.page_id)
                for item in self.children: item.disabled = True
                original_embed = interaction.message.embeds[0]
                original_embed.title = f"[EXCLUÍDO] {original_embed.title}"
                original_embed.color = Color.dark_gray()
                original_embed.description = "Este card foi excluído."
                await interaction.message.edit(embed=original_embed, view=self)
                await inter.followup.send("✅ Card excluído com sucesso!", ephemeral=True)
            except Exception as e: await inter.followup.send(f"🔴 Erro ao excluir o card: {e}", ephemeral=True)

        no_button.callback = lambda inter: inter.response.edit_message(content="❌ Exclusão cancelada.", view=None)
        yes_button.callback = yes_callback
        await interaction.response.send_message("⚠️ **Você tem certeza que deseja excluir este card?**", view=confirm_view, ephemeral=True)


class PaginationView(View):
    def __init__(self, author: discord.Member, results: list, config: dict, notion: NotionIntegration, actions: List[str] = []):
        super().__init__(timeout=300.0)
        self.author, self.results, self.config, self.actions = author, results, config, actions
        self.notion, self.current_page, self.total_pages = notion, 0, len(results)
        if 'edit' not in self.actions: self.remove_item(self.edit_button)
        if 'delete' not in self.actions: self.remove_item(self.delete_button)
        if 'share' not in self.actions: self.remove_item(self.share_button)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Você não pode interagir com os botões de outra pessoa.", ephemeral=True)
            return False
        return True

    def get_current_page_data(self): return self.results[self.current_page]

    async def get_page_embed(self) -> discord.Embed:
        embed = self.notion.format_page_for_embed(page_result=self.get_current_page_data(), display_properties=self.config.get('display_properties', []), include_footer=True)
        embed.set_footer(text=f"Card {self.current_page + 1} de {self.total_pages}")
        return embed

    def update_nav_buttons(self):
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    @discord.ui.button(label="⬅️", style=ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: Interaction, button: Button):
        if self.current_page > 0: self.current_page -= 1
        self.update_nav_buttons()
        await interaction.response.edit_message(embed=await self.get_page_embed(), view=self)

    @discord.ui.button(label="➡️", style=ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: Interaction, button: Button):
        if self.current_page < self.total_pages - 1: self.current_page += 1
        self.update_nav_buttons()
        await interaction.response.edit_message(embed=await self.get_page_embed(), view=self)

    @discord.ui.button(label="✏️ Editar", style=ButtonStyle.primary, row=1)
    async def edit_button(self, interaction: Interaction, button: Button):
        await interaction.response.send_message("Iniciando modo de edição...", ephemeral=True)
        await start_editing_flow(interaction, self.get_current_page_data()['id'], self.config, self.notion)

    @discord.ui.button(label="🗑️ Excluir", style=ButtonStyle.danger, row=1)
    async def delete_button(self, interaction: Interaction, button: Button):
        page_id = self.get_current_page_data()['id']
        confirm_view = View(timeout=60.0)
        yes_button, no_button = Button(label="Sim, excluir!", style=ButtonStyle.danger), Button(label="Cancelar", style=ButtonStyle.secondary)
        confirm_view.add_item(yes_button); confirm_view.add_item(no_button)
        
        async def yes_callback(inter: Interaction):
            await inter.response.defer(ephemeral=True, thinking=True)
            try:
                self.notion.delete_page(page_id)
                await interaction.edit_original_response(content="✅ Card excluído com sucesso.", view=None, embed=None)
                await inter.followup.send("Confirmado!", ephemeral=True)
            except Exception as e: await inter.followup.send(f"🔴 Erro ao excluir: {e}", ephemeral=True)

        no_button.callback = lambda inter: inter.response.edit_message(content="❌ Exclusão cancelada.", view=None)
        yes_button.callback = yes_callback
        await interaction.response.send_message("⚠️ **Tem certeza que deseja excluir?**", view=confirm_view, ephemeral=True)

    @discord.ui.button(label="📢 Exibir para Todos", style=ButtonStyle.success, row=2)
    async def share_button(self, interaction: Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        page_data = self.get_current_page_data()
        share_embed = self.notion.format_page_for_embed(page_data, self.config.get('display_properties', []))
        if share_embed:
            action_view = CardActionView(interaction.user.id, page_data['id'], self.config, self.notion) if self.config.get('action_buttons_enabled', True) else None
            await interaction.channel.send(f"{interaction.user.mention} compartilhou:", embed=share_embed, view=action_view)
            await interaction.followup.send("✅ Card exibido no canal!", ephemeral=True)
        else: await interaction.followup.send("❌ Não foi possível gerar o embed.", ephemeral=True)


class SearchModal(Modal):
    def __init__(self, notion: NotionIntegration, config: dict, selected_property: dict):
        self.notion, self.config, self.selected_property = notion, config, selected_property
        super().__init__(title=f"Buscar por '{self.selected_property['name']}'")
        self.search_term_input = TextInput(label="Digite o termo para procurar", style=discord.TextStyle.short, placeholder="Ex: 'Card de Teste'", required=True)
        self.add_item(self.search_term_input)

    async def on_submit(self, interaction: Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            cards = self.notion.search_in_database(self.config['notion_url'], self.search_term_input.value, self.selected_property['name'], self.selected_property['type'])
            results = cards.get('results', [])
            if not results: return await interaction.followup.send(f"❌ Nenhum resultado para **'{self.search_term_input.value}'**.", ephemeral=True)
            
            await interaction.followup.send(f"✅ **{len(results)}** resultado(s) encontrado(s)!", ephemeral=True)
            view = PaginationView(interaction.user, results, self.config, self.notion, actions=['edit', 'delete', 'share'])
            view.update_nav_buttons()
            await interaction.followup.send(embed=await view.get_page_embed(), view=view, ephemeral=True)
        except Exception as e: await interaction.followup.send(f"🔴 **Erro inesperado:**\n`{e}`", ephemeral=True)


class PublishView(View):
    def __init__(self, author_id: int, embed_to_publish: discord.Embed, page_id: str, config: dict, notion: NotionIntegration):
        super().__init__(timeout=300.0)
        self.author_id, self.embed, self.page_id, self.config, self.notion = author_id, embed_to_publish, page_id, config, notion

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Você não pode interagir com o menu de outra pessoa.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📢 Exibir para Todos", style=ButtonStyle.primary)
    async def publish(self, interaction: Interaction, button: Button):
        button.disabled = True
        
        if interaction.response.is_done():
            await interaction.edit_original_response(content="✅ Card publicado no tópico!", view=self)
        else:
            await interaction.response.edit_message(content="✅ Card publicado no tópico!", view=self)

        action_view = CardActionView(self.author_id, self.page_id, self.config, self.notion) if self.config.get('action_buttons_enabled', True) else None
        await interaction.channel.send(embed=self.embed, view=action_view)
        self.stop()


class CardSelectPropertiesView(View):
    def __init__(self, author_id: int, config: dict, all_properties: list, select_props: list, collected_from_modal: dict, thread_context: Optional[discord.Thread], notion: NotionIntegration):
        super().__init__(timeout=300.0)
        self.author_id, self.config, self.all_properties, self.select_props = author_id, config, all_properties, select_props
        self.collected_properties, self.thread_context, self.notion = collected_from_modal.copy(), thread_context, notion

        for prop in self.select_props:
            options = [SelectOption(label=opt) for opt in prop.get('options', [])[:25]]
            is_multi = prop['type'] == 'multi_select'
            placeholder = "Escolha uma ou mais opções..." if is_multi else "Escolha uma opção..."
            select_menu = Select(placeholder=f"{placeholder} para {prop['name']}", options=options, max_values=len(options) if is_multi else 1, min_values=0, custom_id=f"select_{prop['name']}")
            select_menu.callback = self.on_select_callback
            self.add_item(select_menu)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Você não pode interagir com o menu de outra pessoa.", ephemeral=True)
            return False
        return True

    async def on_select_callback(self, interaction: Interaction):
        prop_name = interaction.data['custom_id'].replace("select_", "")
        values = interaction.data.get('values', [])
        self.collected_properties[prop_name] = values if len(values) > 1 else (values[0] if values else None)
        await interaction.response.defer()

    @discord.ui.button(label="✅ Criar Card", style=ButtonStyle.green, row=4)
    async def confirm_button(self, interaction: Interaction, button: Button):
        for item in self.children: item.disabled = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            title_prop = next((p for p in self.all_properties if p['type'] == 'title'), None)
            if not title_prop: raise NotionAPIError("Nenhuma propriedade de Título foi encontrada.")

            title_value = self.collected_properties.pop(title_prop['name'], f"Card criado em {datetime.now().strftime('%d/%m')}")
            
            if self.config.get('individual_person_prop'): self.collected_properties[self.config.get('individual_person_prop')] = interaction.user.display_name
            if self.config.get('topic_link_property_name') and self.thread_context: self.collected_properties[self.config.get('topic_link_property_name')] = self.thread_context.jump_url
            if self.config.get('collective_person_prop') and self.thread_context:
                participants = await get_topic_participants(self.thread_context)
                self.collected_properties[self.config.get('collective_person_prop')] = [uid for uid in [self.notion.search_id_person(m.display_name) for m in participants] if uid]

            page_content = await _build_notion_page_content(self.config, self.thread_context, self.notion, command_name="card")
            page_properties = self.notion.build_page_properties(self.config['notion_url'], title_value, self.collected_properties)
            response = self.notion.insert_into_database(self.config['notion_url'], page_properties, children=page_content)

            if self.config.get('rename_topic_enabled') and self.thread_context and not self.thread_context.name.startswith("[Card]"):
                await self.thread_context.edit(name=f"[Card] {self.thread_context.name}")

            await interaction.edit_original_response(content="✅ Card criado! Veja abaixo.", view=None)
            success_embed = self.notion.format_page_for_embed(response, self.config.get('display_properties', []))
            success_embed.title = f"✅ Card '{success_embed.title.replace('📌 ', '')}' Criado!"
            success_embed.color = Color.purple()
            publish_view = PublishView(interaction.user.id, success_embed, response['id'], self.config, self.notion)
            await interaction.followup.send("Use o botão para exibir para todos.", embed=success_embed, view=publish_view, ephemeral=True)

        except Exception as e: await interaction.followup.send(f"🔴 **Erro inesperado:**\n`{e}`", ephemeral=True)


class CardModal(Modal):
    def __init__(self, notion: NotionIntegration, config: dict, all_properties: list, text_props: list, select_props: list, thread_context: Optional[discord.Thread], topic_title: Optional[str]):
        super().__init__(title="Criar Novo Card (Etapa 1)")
        self.notion, self.config, self.all_properties, self.text_props, self.select_props, self.thread_context = notion, config, all_properties, text_props, select_props, thread_context
        self.text_inputs = {}
        for prop in self.text_props:
            style = discord.TextStyle.paragraph if any(k in prop['name'].lower() for k in ["desc", "detalhe"]) else discord.TextStyle.short
            self.text_inputs[prop['name']] = TextInput(label=prop['name'], style=style, required=(prop['type'] == 'title'), default=(topic_title if prop['type'] == 'title' else None))
            self.add_item(self.text_inputs[prop['name']])

    async def on_submit(self, interaction: Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        collected = {name: item.value for name, item in self.text_inputs.items() if item.value}
        
        if not self.select_props:
            try:
                title_prop = next((p for p in self.all_properties if p['type'] == 'title'), None)
                title_value = collected.pop(title_prop['name'], "Card sem título")

                if self.config.get('individual_person_prop'): collected[self.config.get('individual_person_prop')] = interaction.user.display_name
                if self.config.get('topic_link_property_name') and self.thread_context: collected[self.config.get('topic_link_property_name')] = self.thread_context.jump_url
                if self.config.get('collective_person_prop') and self.thread_context:
                     participants = await get_topic_participants(self.thread_context)
                     collected[self.config.get('collective_person_prop')] = [uid for uid in [self.notion.search_id_person(m.display_name) for m in participants] if uid]

                page_content = await _build_notion_page_content(self.config, self.thread_context, self.notion, command_name="card")
                page_properties = self.notion.build_page_properties(self.config['notion_url'], title_value, collected)
                response = self.notion.insert_into_database(self.config['notion_url'], page_properties, children=page_content)
                
                if self.config.get('rename_topic_enabled') and self.thread_context and not self.thread_context.name.startswith("[Card]"):
                    await self.thread_context.edit(name=f"[Card] {self.thread_context.name}")

                final_embed = self.notion.format_page_for_embed(response, self.config.get('display_properties', []))
                final_embed.title = f"✅ Card '{final_embed.title.replace('📌 ', '')}' Criado!"
                final_embed.color = Color.purple()
                publish_view = PublishView(interaction.user.id, final_embed, response['id'], self.config, self.notion)
                await interaction.followup.send("Card criado! Use o botão para exibir.", embed=final_embed, view=publish_view, ephemeral=True)

            except Exception as e: await interaction.followup.send(f"🔴 Erro ao criar card: {e}", ephemeral=True)
        else:
            await interaction.edit_original_response(content="📝 Etapa 1/2 concluída. Agora, selecione os valores abaixo.", view=None)
            view = CardSelectPropertiesView(interaction.user.id, self.config, self.all_properties, self.select_props, collected, self.thread_context, self.notion)
            await interaction.followup.send(view=view, ephemeral=True)


class ContinueEditingView(View):
    def __init__(self, author_id: int):
        super().__init__(timeout=180.0)
        self.author_id, self.choice = author_id, None
    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Você não pode interagir com o menu de outra pessoa.", ephemeral=True)
            return False
        return True
    @discord.ui.button(label="✏️ Editar outra", style=ButtonStyle.secondary)
    async def continue_editing(self, interaction: Interaction, button: Button):
        self.choice = 'continue'
        await interaction.response.edit_message(content="Continuando...", view=None)
        self.stop()
    @discord.ui.button(label="✅ Concluir", style=ButtonStyle.success)
    async def finish_editing(self, interaction: Interaction, button: Button):
        self.choice = 'finish'
        await interaction.response.edit_message(content="Finalizando...", view=None)
        self.stop()


class PersonSelectView(View):
    def __init__(self, guild_id: int, channel_id: int, compatible_props: list, config_key: str):
        super().__init__(timeout=180.0)
        options = [SelectOption(label=p['name'], description=f"Tipo: {p['type']}") for p in compatible_props[:25]]
        prop_select = Select(placeholder="Selecione a propriedade de Pessoa...", options=options)
        async def select_callback(interaction: Interaction):
            save_config(guild_id, channel_id, {config_key: interaction.data['values'][0]})
            await interaction.response.edit_message(content=f"✅ Configuração salva!", view=None)
        prop_select.callback = select_callback
        self.add_item(prop_select)


class TopicLinkView(View):
    def __init__(self, guild_id: int, channel_id: int, compatible_props: list):
        super().__init__(timeout=180.0)
        options = [SelectOption(label=p['name'], description=f"Tipo: {p['type']}") for p in compatible_props[:25]]
        prop_select = Select(placeholder="Selecione a propriedade para o link...", options=options)
        async def select_callback(interaction: Interaction):
            save_config(guild_id, channel_id, {'topic_link_property_name': interaction.data['values'][0]})
            await interaction.response.edit_message(content=f"✅ O link será salvo na propriedade selecionada.", view=None)
        prop_select.callback = select_callback
        self.add_item(prop_select)


class ResolvedPropertyDefaultModal(Modal, title="Definir Valor Padrão"):
    def __init__(self, property_name: str, current_value: Optional[str] = None):
        super().__init__()
        self.property_name = property_name
        self.value_input = TextInput(
            label=f"Valor para '{property_name}'",
            placeholder="Ex: Concluído, Finalizado, etc.",
            default=current_value,
            required=True
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: Interaction):
        self.value = self.value_input.value
        await interaction.response.defer()
        self.stop()

class ResolvedConfigView(View):
    def __init__(self, parent_interaction: Interaction, notion: NotionIntegration, config: dict):
        super().__init__(timeout=180.0)
        self.parent_interaction = parent_interaction
        self.guild_id = parent_interaction.guild_id
        self.channel_id = parent_interaction.channel.parent_id if isinstance(parent_interaction.channel, discord.Thread) else parent_interaction.channel.id
        self.notion = notion
        self.config = config
        self.all_db_properties = self.notion.get_properties_for_interaction(self.config['notion_url'])

    async def _update_message(self, interaction: Interaction):
        self.config = load_config(self.guild_id, self.channel_id)
        defaults = self.config.get('resolved_command_defaults', {})
        
        embed = discord.Embed(title="⚙️ Configuração do /resolvido", color=Color.orange())
        if not defaults:
            embed.description = "Nenhuma propriedade com valor padrão foi configurada ainda."
        else:
            desc = "Quando `/resolvido` for usado, as seguintes propriedades serão definidas:\n\n"
            for key, value in defaults.items():
                if isinstance(value, list): value_str = ", ".join(f"`{v}`" for v in value)
                else: value_str = f"`{value}`"
                desc += f"🔹 **{key}**: {value_str}\n"
            embed.description = desc
        
        await self.parent_interaction.edit_original_response(embed=embed, view=self)


    @discord.ui.button(label="Adicionar/Editar Propriedade", style=ButtonStyle.success, emoji="➕")
    async def add_property(self, interaction: Interaction, button: Button):
        
        prop_options = [SelectOption(label=p['name']) for p in self.all_db_properties if p['type'] != 'title']
        prop_select = Select(placeholder="Escolha a propriedade para definir um valor...", options=prop_options[:25])

        async def select_callback(inter: Interaction):
            selected_prop_name = inter.data['values'][0]
            prop_details = next((p for p in self.all_db_properties if p['name'] == selected_prop_name), None)
            
            if prop_details and prop_details['type'] in ['select', 'multi_select', 'status']:
                options = prop_details.get('options', [])
                is_multi = prop_details['type'] == 'multi_select'
                
                value_select = Select(
                    placeholder=f"Escolha o valor padrão para '{selected_prop_name}'...",
                    options=[SelectOption(label=opt) for opt in options[:25]],
                    max_values=len(options) if is_multi else 1
                )

                async def value_select_callback(value_inter: Interaction):
                    chosen_value = value_inter.data['values']
                    current_defaults = self.config.get('resolved_command_defaults', {})
                    current_defaults[selected_prop_name] = chosen_value if is_multi else chosen_value[0]
                    save_config(self.guild_id, self.channel_id, {'resolved_command_defaults': current_defaults})
                    await value_inter.response.edit_message(content=f"✅ Valor padrão para **{selected_prop_name}** salvo!", view=None, delete_after=5)
                    await self._update_message(interaction)

                value_select.callback = value_select_callback
                view = View().add_item(value_select)
                await inter.response.edit_message(content="Agora, escolha o valor padrão:", view=view)

            else:
                current_defaults = self.config.get('resolved_command_defaults', {})
                modal = ResolvedPropertyDefaultModal(selected_prop_name, current_defaults.get(selected_prop_name))
                await inter.response.send_modal(modal)
                await modal.wait()

                if hasattr(modal, 'value'):
                    current_defaults[selected_prop_name] = modal.value
                    save_config(self.guild_id, self.channel_id, {'resolved_command_defaults': current_defaults})
                    await inter.followup.send(f"✅ Valor padrão para **{selected_prop_name}** salvo!", ephemeral=True, delete_after=5)
                    await self._update_message(interaction)
        
        prop_select.callback = select_callback
        view = View().add_item(prop_select)
        await interaction.response.send_message("Primeiro, selecione a propriedade:", view=view, ephemeral=True)


    @discord.ui.button(label="Remover Propriedade", style=ButtonStyle.danger, emoji="🗑️")
    async def remove_property(self, interaction: Interaction, button: Button):
        defaults = self.config.get('resolved_command_defaults', {})
        if not defaults:
            return await interaction.response.send_message("❌ Nenhuma propriedade configurada para remover.", ephemeral=True, delete_after=10)

        prop_options = [SelectOption(label=name) for name in defaults.keys()]
        prop_select = Select(placeholder="Escolha a propriedade para remover...", options=prop_options[:25])

        async def select_callback(inter: Interaction):
            prop_to_remove = inter.data['values'][0]
            current_defaults = self.config.get('resolved_command_defaults', {})
            if prop_to_remove in current_defaults:
                del current_defaults[prop_to_remove]
                save_config(self.guild_id, self.channel_id, {'resolved_command_defaults': current_defaults})
                await inter.response.send_message(f"✅ Propriedade **{prop_to_remove}** removida.", ephemeral=True, delete_after=5)
                await self._update_message(interaction)
        
        prop_select.callback = select_callback
        view = View().add_item(prop_select)
        await interaction.response.send_message("Selecione a propriedade para remover:", view=view, ephemeral=True)


    @discord.ui.button(label="Voltar", style=ButtonStyle.secondary, emoji="↩️")
    async def go_back(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        main_view = ManagementView(self.parent_interaction, self.notion, self.config)
        await self.parent_interaction.edit_original_response(content="Este canal já está configurado. Escolha uma opção de gerenciamento:", embed=None, view=main_view)

class CardContentView(View):
    def __init__(self, parent_interaction: Interaction, notion: NotionIntegration, config: dict):
        super().__init__(timeout=180.0)
        self.parent_interaction = parent_interaction
        self.guild_id = parent_interaction.guild_id
        self.channel_id = parent_interaction.channel.parent_id if isinstance(parent_interaction.channel, discord.Thread) else parent_interaction.channel.id
        self.notion = notion
        self.config = config
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()
        self.add_item(Button(label="Configurar Resumo por IA", custom_id="config_ai_summary", style=ButtonStyle.secondary, emoji="✨"))
        self.add_item(Button(label="Configurar Captura de 1ª Mensagem", custom_id="config_first_message", style=ButtonStyle.secondary, emoji="✉️"))
        self.add_item(Button(label="Voltar", custom_id="back_to_main", style=ButtonStyle.grey, row=2))

    async def interaction_check(self, interaction: Interaction) -> bool:
        # CORREÇÃO APLICADA AQUI
        custom_id = interaction.data.get('custom_id')

        if custom_id == "config_ai_summary":
            await self.configure_feature(interaction, 'ai_summary_for_commands', 'Resumo por IA')
            return False 
        elif custom_id == "config_first_message":
            await self.configure_feature(interaction, 'capture_first_message_for_commands', 'Captura da 1ª Mensagem')
            return False
        elif custom_id == "back_to_main":
            main_view = ManagementView(self.parent_interaction, self.notion, self.config)
            await self.parent_interaction.edit_original_response(content="Este canal já está configurado. Escolha uma opção de gerenciamento:", embed=None, view=main_view)
            await interaction.response.defer()
            return False
        return True

    async def configure_feature(self, interaction: Interaction, config_key: str, feature_name: str):
        current_setting = self.config.get(config_key, [])
        options = [
            SelectOption(label="/card", value="card", default=("card" in current_setting)),
            SelectOption(label="/resolvido", value="resolvido", default=("resolvido" in current_setting))
        ]
        select_menu = Select(placeholder=f"Ativar {feature_name} para...", options=options, min_values=0, max_values=2, custom_id=f"select_{config_key}")

        async def select_callback(inter: Interaction):
            save_config(self.guild_id, self.channel_id, {config_key: inter.data.get('values', [])})
            self.config = load_config(self.guild_id, self.channel_id)
            await inter.response.edit_message(content=f"✅ Configuração de **{feature_name}** atualizada!", view=None, delete_after=5)
            await self.update_embed(self.parent_interaction)

        select_menu.callback = select_callback
        view = View().add_item(select_menu)
        await interaction.response.send_message(f"Selecione para quais comandos a função **{feature_name}** deve ser ativada.", view=view, ephemeral=True)

    async def update_embed(self, interaction: Interaction):
        ai_commands = self.config.get('ai_summary_for_commands', [])
        fm_commands = self.config.get('capture_first_message_for_commands', [])
        ai_status = ", ".join(f"`/{cmd}`" for cmd in ai_commands) if ai_commands else "Nenhum"
        fm_status = ", ".join(f"`/{cmd}`" for cmd in fm_commands) if fm_commands else "Nenhum"
        
        embed = discord.Embed(title="⚙️ Configurar Conteúdo do Card", color=Color.blue())
        embed.add_field(name="Resumo por IA", value=f"Ativado para: {ai_status}", inline=False)
        embed.add_field(name="Captura da 1ª Mensagem", value=f"Ativado para: {fm_status}", inline=False)
        
        await self.parent_interaction.edit_original_response(embed=embed, view=self)


class ManagementView(View):
    def __init__(self, parent_interaction: Interaction, notion: NotionIntegration, config: dict):
        super().__init__(timeout=180.0)
        self.parent_interaction = parent_interaction
        self.guild_id = parent_interaction.guild_id
        self.channel_id = parent_interaction.channel.parent_id if isinstance(parent_interaction.channel, discord.Thread) else parent_interaction.channel.id
        self.notion, self.config = notion, config

    @discord.ui.button(label="Reconfigurar URL", style=ButtonStyle.primary, emoji="🔄", row=0)
    async def reconfigure(self, interaction: Interaction, button: Button):
        await interaction.response.send_message("Para reconfigurar, use `/config` novamente com a nova URL.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Botões de Ação", style=ButtonStyle.secondary, emoji="⚙️", row=0)
    async def manage_buttons(self, interaction: Interaction, button: Button):
        is_enabled = self.config.get('action_buttons_enabled', True)
        toggle_view = View(timeout=60.0)
        button_label = "Desativar Botões" if is_enabled else "Ativar Botões"
        toggle_button = Button(label=button_label, style=ButtonStyle.danger if is_enabled else ButtonStyle.success)
        async def t_callback(inter: Interaction):
            new_state = not is_enabled
            save_config(self.guild_id, self.channel_id, {'action_buttons_enabled': new_state})
            await inter.response.edit_message(content=f"✅ Botões de ação **{'ATIVADOS' if new_state else 'DESATIVADOS'}**.", view=None)
        toggle_button.callback = t_callback
        toggle_view.add_item(toggle_button)
        await interaction.response.send_message(f"Botões estão **{'ATIVADOS' if is_enabled else 'DESATIVADOS'}**.", view=toggle_view, ephemeral=True)

    @discord.ui.button(label="Renomear Tópico", style=ButtonStyle.secondary, emoji="✍️", row=0)
    async def manage_rename_topic(self, interaction: Interaction, button: Button):
        is_enabled = self.config.get('rename_topic_enabled', False)
        toggle_view = View(timeout=60.0)
        button_label = "Desativar Renomeação" if is_enabled else "Ativar Renomeação"
        toggle_button = Button(label=button_label, style=ButtonStyle.danger if is_enabled else ButtonStyle.success)
        async def t_callback(inter: Interaction):
            new_state = not is_enabled
            save_config(self.guild_id, self.channel_id, {'rename_topic_enabled': new_state})
            await inter.response.edit_message(content=f"✅ Renomeação de tópico **{'ATIVADA' if new_state else 'DESATIVADA'}**.", view=None)
        toggle_button.callback = t_callback
        toggle_view.add_item(toggle_button)
        await interaction.response.send_message(f"Renomeação de tópicos está **{'ATIVADA' if is_enabled else 'DESATIVADA'}**.", view=toggle_view, ephemeral=True)

    @discord.ui.button(label="Conteúdo do Card", style=ButtonStyle.secondary, emoji="📝", row=1)
    async def manage_content(self, interaction: Interaction, button: Button):
        view = CardContentView(interaction, self.notion, self.config)
        await interaction.response.defer()
        await view.update_embed(interaction)

    @discord.ui.button(label="Configurar Link de Tópico", style=ButtonStyle.secondary, emoji="🔗", row=2)
    async def configure_topic_link(self, interaction: Interaction, button: Button):
        all_props = self.notion.get_properties_for_interaction(self.config['notion_url'])
        compat_props = [p for p in all_props if p['type'] in ['rich_text', 'url']]
        if not compat_props: return await interaction.response.send_message("❌ Nenhuma propriedade compatível (Texto/URL) encontrada.", ephemeral=True)
        view = TopicLinkView(self.guild_id, self.channel_id, compat_props)
        await interaction.response.send_message("Selecione a propriedade para o link.", view=view, ephemeral=True)

    @discord.ui.button(label="Definir Dono do Card", style=ButtonStyle.secondary, emoji="👤", row=3)
    async def configure_individual_person(self, interaction: Interaction, button: Button):
        all_props = self.notion.get_properties_for_interaction(self.config['notion_url'])
        people_props = [p for p in all_props if p['type'] == 'people']
        if not people_props: return await interaction.response.send_message("❌ Nenhuma propriedade 'Pessoa' encontrada.", ephemeral=True)
        view = PersonSelectView(self.guild_id, self.channel_id, people_props, 'individual_person_prop')
        await interaction.response.send_message("Selecione a propriedade para o autor do comando.", view=view, ephemeral=True)

    @discord.ui.button(label="Definir Envolvidos do Tópico", style=ButtonStyle.secondary, emoji="👥", row=3)
    async def configure_collective_person(self, interaction: Interaction, button: Button):
        all_props = self.notion.get_properties_for_interaction(self.config['notion_url'])
        people_props = [p for p in all_props if p['type'] == 'people']
        if not people_props: return await interaction.response.send_message("❌ Nenhuma propriedade 'Pessoa' encontrada.", ephemeral=True)
        view = PersonSelectView(self.guild_id, self.channel_id, people_props, 'collective_person_prop')
        await interaction.response.send_message("Selecione a propriedade para os participantes do tópico.", view=view, ephemeral=True)
    
    @discord.ui.button(label="Configurar /resolvido", style=ButtonStyle.primary, emoji="✅", row=4)
    async def configure_resolved_command(self, interaction: Interaction, button: Button):
        view = ResolvedConfigView(interaction, self.notion, self.config)
        await interaction.response.defer()
        await view._update_message(interaction)