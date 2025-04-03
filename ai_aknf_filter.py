# -*- coding: utf-8 -*-
# Gemini AKNF Roms Filter Script
# Version: 1.0.15 (Switched to lxml for XML processing)
# Filters No-Intro / Redump DAT files based on community recommendations and estimated quality,
# using Google Gemini API. AKNF = All Killers, No Fillers.

# --- Raccomandazione ---
# Per risultati ottimali, si raccomanda l'uso di file DAT in formato 1G1R (One Game One ROM),
# preferibilmente generati o processati con Retool (https://github.com/unexpectedpanda/retool).
# Questo aiuta a ridurre la ridondanza e a focalizzare il filtro sui titoli unici più rilevanti.

# --- Dipendenze ---
# Questo script richiede l'installazione di librerie esterne:
# pip install google-generativeai python-dotenv lxml rich
# (rich è opzionale per output colorato)

import os
import argparse
# Importa lxml.etree e lo usa come ET per minimizzare le modifiche al codice esistente
try:
    import lxml.etree as ET
    LXML_AVAILABLE = True
except ImportError:
    # Fallback a ElementTree standard se lxml non è installato
    import xml.etree.ElementTree as ET
    LXML_AVAILABLE = False
import re
import google.generativeai as genai
from pathlib import Path
from dotenv import load_dotenv
import sys
import time
import html
import threading
import itertools

# --- Script Configuration ---
SCRIPT_VERSION = "1.0.15" # Updated version

# Importa Rich per output colorato
try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.panel import Panel
    from rich.text import Text
    console = Console()
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    # Fallback a print normale se rich non è installato
    class ConsoleFallback:
        def print(self, *args, **kwargs):
            filtered_args = []
            for arg in args:
                try:
                    filtered_args.append(str(arg))
                except Exception:
                    filtered_args.append(f"[Impossibile convertire l'argomento: {type(arg)}]")
            kwargs.pop('style', None)
            print(*filtered_args, **kwargs)

    console = ConsoleFallback()
    Panel = lambda text, title, border_style: f"\n--- {title} ---\n{text}\n-------------"
    Text = str

# Avviso se lxml non è disponibile
if not LXML_AVAILABLE:
    console.print("[yellow]Avviso:[/yellow] Libreria 'lxml' non trovata. Uso 'xml.etree.ElementTree' standard.")
    console.print("         Si raccomanda 'lxml' per performance e robustezza: [bold]pip install lxml[/bold]")


# --- Configurazione API Gemini ---
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

if not API_KEY:
    console.print("Errore: GOOGLE_API_KEY non trovata nel file .env", style="bold red")
    sys.exit(1)

genai.configure(api_key=API_KEY)

# Modello Gemini da usare
GEMINI_MODEL_NAME = "gemini-2.0-flash" # Modello specificato dall'utente

# Configurazione Generazione
generation_config = {
  "temperature": 0.2,
  "top_p": 0.95,
  "top_k": 64,
  "max_output_tokens": 8192,
  "response_mime_type": "text/plain",
}
safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# --- Prompt Template per Gemini ---
PROMPT_TEMPLATE = """
OBIETTIVO: Filtrare il seguente file DAT XML per la console **{console_name}** di **{console_maker}**. Voglio mantenere SOLO i giochi che soddisfano **ALMENO UNO** dei seguenti criteri (con **priorità assoluta** data al Criterio 1):

1.  **Raccomandati dalla Community (/v/):** Il gioco (considerando il suo nome base e le sue *varianti di titolo regionali note*) è generalmente considerato una raccomandazione chiave dalla community (es. presente nelle liste `/v/ Recommended Games Wiki` per la console specificata - usa la tua conoscenza interna). **Se un gioco soddisfa questo criterio, DEVE essere incluso, indipendentemente dagli altri criteri.**
2.  **Acclamati dalla Critica (Ufficiali):** Il gioco è una *release ufficiale* (considerando tutte le sue varianti regionali) con un'alta acclamazione critica storica (punteggio stimato **>= {score_threshold}/100** o universalmente riconosciuto come "must-play" per la piattaforma).
3.  **Giochi Non Ufficiali di Qualità:** Il gioco è un *homebrew, hack, traduzione fan, o port non ufficiale* **altamente considerato, ben recensito, o raccomandato** dalla community, rappresentando un'aggiunta di alta qualità.

ISTRUZIONI DETTAGLIATE:

* **Input:** Il file DAT XML fornito di seguito contiene l'header originale e una lista di tag `<game name="...">` che rappresentano TUTTI i giochi disponibili nel DAT originale completo. Il nome console (`{console_name}`) e il produttore (`{console_maker}`) sono stati estratti dall'header.
* **Confronto & Selezione:**
    * Identifica ogni gioco dall'attributo `name`.
    * **Dai priorità assoluta ai giochi che soddisfano il Criterio 1.** Includili sempre.
    * Per gli altri giochi, considera le **varianti di titolo regionali** (es. Spyro 2 USA vs Europe) come lo stesso gioco concettuale per i criteri 1 e 2.
    * Se un gioco concettuale (non già incluso tramite Criterio 1) soddisfa il Criterio 2 (con soglia >= {score_threshold}/100) OPPURE il Criterio 3, includi il relativo tag `<game name="...">` (esattamente come fornito nell'input) nella tua risposta.
* **Output:** Genera un NUOVO file DAT XML contenente **SOLO** l'elemento `<header>` originale (che ti fornisco) e di seguito **SOLO i tag `<game name="...">`** dei giochi selezionati. NON includere nessun altro dato dentro i tag `<game>`. L'output deve essere XML valido e ben formato. Assicurati che i caratteri speciali nell'attributo `name` siano correttamente escapati (`&amp;`, `&lt;`, `&gt;`, `&quot;`, `&apos;`).
* **IMPORTANTE:** Rispondi SOLO con il contenuto XML, senza introduzioni, spiegazioni o marker ```xml ```. Inizia direttamente con `<?xml version='1.0' encoding='utf-8'?>` o `<datafile>`.

FILE DAT "COMPRESSO" DA FILTRARE:

```xml
{compressed_dat_content}
```
"""

# --- Funzioni Helper ---

def extract_console_details(raw_name):
    """Pulisce il nome grezzo, rimuove parentesi e divide in produttore e nome console."""
    if raw_name is None:
        return "Sconosciuto", "Nome Console Sconosciuto"
    original_name_for_debug = raw_name
    try:
        cleaned_name = re.sub(r'\s*\([^)]*?\)\s*', ' ', raw_name).strip()
        cleaned_name = re.sub(r'\s{2,}', ' ', cleaned_name).strip()
        parts = cleaned_name.split(' - ', 1)
        if len(parts) == 2:
            maker = parts[0].strip()
            name = parts[1].strip()
            if not maker: maker = "Produttore Sconosciuto"
            return maker, name
        else:
            maker = "Produttore Sconosciuto"
            name = cleaned_name
            return maker, name
    except Exception as e:
        try:
            console.print(f"[yellow]Avviso: Errore estrazione dettagli console da '{original_name_for_debug}': {e}. Uso fallback.[/yellow]", style="yellow")
        except TypeError:
            console.print(f"Avviso: Errore estrazione dettagli console da '{original_name_for_debug}': {e}. Uso fallback.")
        return "Sconosciuto", original_name_for_debug

def escape_xml_attribute(value):
    """Escape special characters for XML attributes using html library."""
    if value is None: return ""
    return html.escape(value, quote=True)

def parse_and_compress_dat(filepath):
    """Legge DAT, estrae header, nomi giochi, dati originali e conteggio."""
    original_games_data = {}
    compressed_game_tags = []
    console_maker = "Sconosciuto"
    console_name = "Nome Console Sconosciuto"
    original_header_xml = "<header><name>Header Mancante</name><description>Header originale non trovato o illeggibile.</description></header>"
    game_count_original = 0
    root = None
    console_name_raw = "[Nome non estratto]"

    console.print(f"   Parsing file DAT: [cyan]{filepath.name}[/cyan]")
    try:
        # Usa lxml per il parsing se disponibile, altrimenti fallback a ET standard
        # recover=True tenta di recuperare da errori XML (solo lxml)
        parser_engine = ET.XMLParser(encoding='utf-8', recover=(LXML_AVAILABLE))
        try:
            # Parsing completo per estrarre header e giochi
            tree = ET.parse(filepath, parser=parser_engine)
            root = tree.getroot()

            # Estrazione header
            header_element = root.find('header')
            if header_element is not None:
                # Ricostruisce l'XML dell'header originale (senza pretty print qui)
                original_header_xml = ET.tostring(header_element, encoding='unicode', method='xml')
                name_elem = header_element.find('name')
                if name_elem is not None and name_elem.text is not None:
                    console_name_raw = name_elem.text
                    console_maker, console_name = extract_console_details(console_name_raw)
                else:
                    console.print("[yellow]Avviso:[/yellow] Tag <name> non trovato o vuoto nell'header.")
                    console_name_raw = "[Tag <name> mancante/vuoto]"
            else:
                console.print("[red]Errore:[/red] Tag <header> non trovato nel file DAT.")

        except ET.ParseError as e_parse: # Errore di parsing XML
            console.print(f"   [red]Errore FATALE:[/red] Parsing XML fallito: {e_parse}")
            line, col = getattr(e_parse, 'position', ('N/A', 'N/A'))
            console.print(f"   -> Errore vicino a riga: {line}, colonna: {col}")
            return "Sconosciuto", "Errore Parsing XML", None, None, None, 0
        except Exception as e_generic_parse: # Altri errori durante il parsing
             console.print(f"   [red]Errore FATALE:[/red] Errore durante il parsing: {e_generic_parse}")
             return "Sconosciuto", "Errore Parsing Generico", None, None, None, 0

        # Gestione fallback nome console se ancora sconosciuto
        if console_name == "Nome Console Sconosciuto" and console_maker == "Sconosciuto":
             fallback_name_raw = Path(filepath).stem
             console.print(f"[yellow]Avviso:[/yellow] Dettagli console non ricavati ('{console_name_raw}'). Uso nome file: '{fallback_name_raw}'")
             console_maker, console_name = extract_console_details(fallback_name_raw)

        console.print(f"   -> Produttore per IA: [bold yellow]{console_maker}[/bold yellow]")
        console.print(f"   -> Nome Console per IA: [bold magenta]{console_name}[/bold magenta]")

        # Estrazione giochi
        game_elements = root.findall('.//game')
        game_count_original = len(game_elements)
        console.print(f"   -> Trovati [bold cyan]{game_count_original}[/bold cyan] giochi nel DAT originale.")

        for game_elem in game_elements:
            game_name = game_elem.get('name')
            if game_name:
                # Salva l'XML originale del gioco
                game_xml_str = ET.tostring(game_elem, encoding='unicode', method='xml')
                original_games_data[game_name] = game_xml_str
                # Crea il tag compresso per l'IA
                compressed_game_tags.append(f'<game name="{escape_xml_attribute(game_name)}"/>')

        # Costruisci il contenuto XML compresso per l'IA
        header_str = original_header_xml.strip()
        compressed_dat_content = f"<?xml version='1.0' encoding='utf-8'?>\n<datafile>\n{header_str}\n"
        compressed_dat_content += "\n".join(compressed_game_tags)
        compressed_dat_content += "\n</datafile>"

        return console_maker, console_name, original_header_xml, original_games_data, compressed_dat_content, game_count_original

    except Exception as e:
        console.print(f"[red]Errore inaspettato durante parsing/compressione:[/red] {e}")
        import traceback
        traceback.print_exc()
        return "Sconosciuto", "Errore Inaspettato", None, None, None, 0

# Classe per gestire l'animazione di attesa (spinner)
class ProcessingIndicator:
    def __init__(self, message="   Chiamata API Gemini... "):
        self._message = message
        self._thread = None
        self._stop_event = threading.Event()
        self._spinner = itertools.cycle(['|', '/', '-', '\\'])
        self._lock = threading.Lock()

    def _animate(self):
        while not self._stop_event.is_set():
            with self._lock:
                if not self._stop_event.is_set():
                    sys.stdout.write(f"\r{self._message}{next(self._spinner)}")
                    sys.stdout.flush()
            time.sleep(0.15)
        sys.stdout.write('\r' + ' ' * (len(self._message) + 5) + '\r')
        sys.stdout.flush()

    def start(self):
        sys.stdout.write(f"\r{self._message}...")
        sys.stdout.flush()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            with self._lock:
                self._stop_event.set()
            self._thread.join()

def call_gemini_api(prompt_text):
    """Invia il prompt all'API Gemini e ritorna la risposta testuale."""
    indicator = ProcessingIndicator(f"   Chiamata API Gemini ([yellow]{GEMINI_MODEL_NAME}[/yellow])... ")
    indicator.start()
    response = None
    error_message = None
    try:
        model = genai.GenerativeModel(
            GEMINI_MODEL_NAME,
            safety_settings=safety_settings,
            generation_config=generation_config
        )
        response = model.generate_content(prompt_text, request_options={'timeout': 600})
    except Exception as e:
        error_message = f"Errore durante la chiamata API Gemini: {e}"
    finally:
        indicator.stop()

    if error_message:
        console.print(f"\n   [red]Fallita:[/red] {error_message}")
        return None

    if not hasattr(response, 'text') or not response.text:
        reason = "Nessuna risposta testuale ricevuta."
        if hasattr(response, 'prompt_feedback') and response.prompt_feedback and hasattr(response.prompt_feedback, 'block_reason'):
            reason = f"Richiesta bloccata per: {response.prompt_feedback.block_reason}"
        console.print(f"\n   [red]Fallita:[/red] {reason}")
        if hasattr(response, 'prompt_feedback') and hasattr(response.prompt_feedback,'safety_ratings') and response.prompt_feedback.safety_ratings:
             console.print("     Safety Ratings:", style="dim")
             for rating in response.prompt_feedback.safety_ratings:
                  console.print(f"        - {rating.category}: {rating.probability}", style="dim")
        return None

    console.print("\n   [green]Risposta API ricevuta.[/green]")
    text_response = response.text.strip()

    text_response = re.sub(r"^```xml\s*", "", text_response, flags=re.IGNORECASE)
    text_response = re.sub(r"^```\s*", "", text_response)
    text_response = re.sub(r"\s*```$", "", text_response)
    text_response = text_response.strip()

    if not text_response.startswith(("<?xml", "<datafile>")):
        console.print(f"   [red]ERRORE:[/red] La risposta dell'IA non sembra XML valido. Inizio:")
        console.print(f"[dim]{text_response[:500]}...[/dim]")
        try:
            with open("invalid_ai_response_start.xml", "w", encoding="utf-8") as f_inv:
                f_inv.write(text_response)
            console.print("   (Risposta problematica salvata in [filename]invalid_ai_response_start.xml[/filename])", style="yellow")
        except Exception as write_err:
            console.print(f"   [yellow]Avviso:[/yellow] Impossibile salvare invalid_ai_response_start.xml: {write_err}")
        return None

    return text_response

def reconstruct_filtered_dat(ai_response_xml, original_header_xml, original_games_data):
    """Ricostruisce il DAT completo usando la risposta filtrata dell'IA e modifica l'header."""
    console.print("   Ricostruzione DAT filtrato...")
    kept_game_names = []
    ai_root = None
    root_tag_name = "datafile"

    if not ai_response_xml:
        console.print("   [red]ERRORE:[/red] Nessuna risposta XML dall'IA per ricostruire.")
        return None

    try:
        # Usa lxml per parsare la risposta dell'IA se disponibile
        parser_engine_ai = ET.XMLParser(encoding='utf-8', recover=(LXML_AVAILABLE))
        ai_response_clean = re.sub(r"^\s*<\?xml.*?\?>", "", ai_response_xml).strip()
        # Assicurati che l'input sia bytes per lxml
        ai_root = ET.fromstring(ai_response_clean.encode('utf-8'), parser=parser_engine_ai)
        root_tag_name = ai_root.tag

        for game_elem in ai_root.findall('.//game'):
            game_name = game_elem.get('name')
            if game_name:
                kept_game_names.append(game_name)

    except Exception as e:
        console.print(f"   [red]ERRORE FATALE:[/red] Impossibile fare il parsing dei giochi dalla risposta XML dell'IA: {e}")
        console.print(f"   Risposta ricevuta (primi 1000 caratteri):\n[dim]{ai_response_xml[:1000]}[/dim]")
        try:
            with open("failed_ai_response_games.xml", "w", encoding="utf-8") as f_fail:
                f_fail.write(ai_response_xml)
            console.print("   (Risposta fallita salvata in [filename]failed_ai_response_games.xml[/filename])", style="yellow")
        except Exception: pass
        return None

    try:
        # Parsa l'header originale per modificarlo (usa lxml se disponibile)
        parser_engine_header = ET.XMLParser(encoding='utf-8', recover=(LXML_AVAILABLE))
        # Assicurati che l'input sia bytes per lxml
        header_element = ET.fromstring(original_header_xml.encode('utf-8'), parser=parser_engine_header)

        # Modifica/Aggiungi description
        desc_tag = header_element.find('description')
        if desc_tag is not None:
            original_desc = desc_tag.text.strip() if desc_tag.text else ''
            if "(GeminiAKNF)" not in original_desc:
                 desc_tag.text = f"{original_desc} (GeminiAKNF)".strip()
        else:
            desc_tag = ET.SubElement(header_element, 'description')
            desc_tag.text = "(GeminiAKNF)"

        # Rimuovi eventuale tag geminiaknf esistente
        existing_aknf_tag = header_element.find('geminiaknf')
        if existing_aknf_tag is not None:
            header_element.remove(existing_aknf_tag)

        # Crea il nuovo tag geminiaknf
        aknf_tag = ET.Element('geminiaknf')
        aknf_tag.text = f"Created by Gemini AKNF ver. {SCRIPT_VERSION}"

        # Trova l'indice dove inserire il nuovo tag (idealmente dopo <retool>)
        insert_index = -1
        retool_tag = header_element.find('retool')
        if retool_tag is not None:
            try:
                children = list(header_element)
                insert_index = children.index(retool_tag) + 1
            except ValueError:
                insert_index = len(header_element)
        else:
             clrmamepro_tag = header_element.find('clrmamepro')
             if clrmamepro_tag is not None:
                 try:
                     children = list(header_element)
                     insert_index = children.index(clrmamepro_tag)
                 except ValueError:
                     insert_index = len(header_element)
             else:
                 insert_index = len(header_element)

        header_element.insert(insert_index, aknf_tag)

        # Ottieni l'XML dell'header modificato come stringa unicode
        # Usa pretty_print=True con lxml per indentazione automatica
        if LXML_AVAILABLE:
            modified_header_xml = ET.tostring(header_element, encoding='unicode', pretty_print=True, xml_declaration=False)
        else:
            # Fallback senza pretty_print se lxml non è disponibile
            modified_header_xml = ET.tostring(header_element, encoding='unicode', method='xml')


    except Exception as e_header:
        console.print(f"   [yellow]Avviso:[/yellow] Errore modificando l'header originale: {e_header}. Uso header originale non modificato.")
        modified_header_xml = original_header_xml # Fallback

    final_header_str = modified_header_xml.strip()

    # Costruisci il DAT finale
    final_dat_parts = []
    final_dat_parts.append("<?xml version='1.0' encoding='utf-8'?>")
    final_dat_parts.append(f"<{root_tag_name}>")
    final_dat_parts.append(final_header_str)

    found_count = 0
    missing_count = 0
    unique_kept_names = sorted(list(set(kept_game_names)))

    console.print(f"   -> L'IA ha richiesto [bold cyan]{len(unique_kept_names)}[/bold cyan] giochi unici.")

    for name in unique_kept_names:
        original_game_xml = original_games_data.get(name)
        if original_game_xml:
            game_data_cleaned = re.sub(r"^\s*<\?xml.*?\?>", "", original_game_xml).strip()
            # Non aggiungere indentazione manuale qui, lxml dovrebbe averla gestita nell'header
            # e i giochi dovrebbero mantenere la loro formattazione originale o essere gestiti da un formattatore esterno
            final_dat_parts.append(game_data_cleaned)
            found_count += 1
        else:
            console.print(f"   [yellow]Avviso:[/yellow] Gioco '{name}' restituito da IA ma non trovato nel DAT originale. Sarà omesso.")
            missing_count += 1

    final_dat_parts.append(f"</{root_tag_name}>")
    console.print(f"   Ricostruzione completata. Giochi [green]inclusi[/green]: [bold cyan]{found_count}[/bold cyan]. Giochi [yellow]mancanti/omessi[/yellow]: {missing_count}.")
    if missing_count > 0:
        console.print("   -> [dim]I giochi mancanti potrebbero indicare preferenze di versione dell'IA o errori nel parsing/matching dei nomi.[/dim]")

    # Unisce le parti con newline per creare il file finale
    # Usa lxml per un pretty print finale dell'intero documento, se disponibile
    final_xml_string = "\n".join(final_dat_parts)
    if LXML_AVAILABLE:
        try:
            # Riformatta l'intero documento per coerenza
            final_root = ET.fromstring(final_xml_string.encode('utf-8'))
            # Nota: tostring con pretty_print=True aggiunge la dichiarazione XML, quindi non serve aggiungerla manualmente prima
            return ET.tostring(final_root, encoding='unicode', pretty_print=True, xml_declaration=True)
        except Exception as pretty_print_error:
            console.print(f"[yellow]Avviso:[/yellow] Errore durante pretty-printing finale con lxml: {pretty_print_error}")
            # Fallback a stringa non formattata
            return final_xml_string
    else:
        return final_xml_string


def sanitize_filename(filename):
    """Rimuove o sostituisce caratteri non validi per i nomi file cross-platform."""
    sanitized = filename.replace('/', '-').replace('\\', '-')
    sanitized = re.sub(r'[<>:"|?*]', '', sanitized)
    sanitized = re.sub(r'[\x00-\x1f\x7f]', '', sanitized)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    sanitized = sanitized.rstrip('. ')
    MAX_LEN_BYTES = 200
    if len(sanitized.encode('utf-8', 'ignore')) > MAX_LEN_BYTES:
        name, ext = os.path.splitext(sanitized)
        cutoff = MAX_LEN_BYTES - len(ext.encode('utf-8', 'ignore')) - 1
        name_bytes = name.encode('utf-8', 'ignore')
        name = name_bytes[:cutoff].decode('utf-8', 'ignore')
        sanitized = name + "~" + ext
    reserved_names = {"CON", "PRN", "AUX", "NUL",
                      "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                      "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}
    name_part, _ = os.path.splitext(sanitized)
    if name_part.upper() in reserved_names:
        sanitized = "_" + sanitized
    if not sanitized or sanitized.lower() == ".dat":
        sanitized = "Filtered_DAT_File.dat"
    return sanitized


# Modificato per accettare score_threshold
def process_dat_file(filepath, score_threshold):
    """Orchestra il processo completo per un singolo file DAT."""
    start_time = time.time()
    game_count_original = 0

    # Conteggio giochi iniziale (veloce) - Usa lxml iterparse se disponibile
    try:
        game_count_original = 0
        with open(filepath, 'rb') as f:
             # Usa tag='game' se lxml è disponibile, altrimenti controlla nel loop
             tag_filter = 'game' if LXML_AVAILABLE else None
             # Usare 'tag' con iterparse standard genera errore, quindi lo omettiamo se lxml non c'è
             if LXML_AVAILABLE:
                 context = ET.iterparse(f, events=('end',), tag=tag_filter)
             else:
                 context = ET.iterparse(f, events=('end',)) # Senza tag

             for event, elem in context:
                 # Se non si usa lxml o tag filter fallisce (improbabile con lxml), controlla qui
                 if not LXML_AVAILABLE and elem.tag == 'game':
                     game_count_original += 1
                 elif LXML_AVAILABLE: # Se lxml e tag filter attivi, conta direttamente
                     game_count_original += 1

                 # Pulisci l'elemento per liberare memoria
                 # elem.clear() # Rimosso perché può interferire con lxml, che gestisce meglio la memoria
                 # Il codice per pulire i parent non è necessario/compatibile
                 # Tentativo di pulizia più robusto per lxml:
                 if LXML_AVAILABLE:
                    elem.clear()
                    # Elimina riferimenti agli elementi precedenti per liberare memoria
                    while elem.getprevious() is not None:
                        del elem.getparent()[0]
                 else: # Per ET standard
                    elem.clear()

    except ET.ParseError as parse_err:
        console.print(f"[red]Errore parsing XML iniziale per conteggio:[/red] {parse_err} in {filepath.name}")
    except Exception as e:
        console.print(f"[red]Errore lettura iniziale per conteggio:[/red] {e} in {filepath.name}")

    console.rule(f"Inizio Elaborazione: {filepath.name}", style="blue")

    # Parsa DAT e estrai dettagli console
    console_maker, console_name, original_header, games_data, compressed_dat, count_from_parse = parse_and_compress_dat(filepath)

    # Aggiorna conteggio se il primo tentativo è fallito ma il parsing completo ha funzionato
    if game_count_original == 0 and count_from_parse > 0:
        game_count_original = count_from_parse
    # Se anche il secondo conteggio è 0, usa quello iniziale (potrebbe essere 0 ma corretto)
    elif count_from_parse == 0 and game_count_original > 0:
         pass # Mantieni game_count_original
    elif count_from_parse > 0: # Se entrambi > 0, usa quello dal parsing completo (più affidabile)
        game_count_original = count_from_parse


    # Verifica errori critici nel parsing
    if games_data is None or compressed_dat is None:
        console.print(f"[red]Errore critico:[/red] Impossibile parsare/comprimere {filepath.name} (Maker: {console_maker}, Console: {console_name}), saltato.", style="bold red")
        console.rule(style="red")
        return False

    # Prepara il prompt con maker, name e score_threshold
    try:
        prompt = PROMPT_TEMPLATE.format(
            console_maker=console_maker,
            console_name=console_name,
            score_threshold=score_threshold, # Passa la soglia al prompt
            compressed_dat_content=compressed_dat
        )
    except KeyError as fmt_err:
        console.print(f"[red]Errore formattazione prompt:[/red] Chiave mancante: {fmt_err}. Maker='{console_maker}', Name='{console_name}', Threshold='{score_threshold}'")
        console.rule(style="red")
        return False

    # Chiama API Gemini
    ai_response = call_gemini_api(prompt)

    if not ai_response:
        console.print(f"[red]Elaborazione fallita:[/red] Nessuna risposta valida dall'API per {filepath.name}.", style="bold red")
        console.rule(style="red")
        return False

    # Ricostruisci DAT finale
    final_dat_content = reconstruct_filtered_dat(ai_response, original_header, games_data)

    if not final_dat_content:
        console.print(f"[red]Elaborazione fallita:[/red] Errore durante la ricostruzione del DAT per {filepath.name}.", style="bold red")
        console.rule(style="red")
        return False

    # Salva file finale - Usando il formato Maker - Name
    output_filename_base = f"{console_maker} - {console_name} (GeminiAKNF {SCRIPT_VERSION}).dat"
    safe_output_filename = sanitize_filename(output_filename_base)
    output_path = filepath.parent / safe_output_filename

    try:
        with open(output_path, 'w', encoding='utf-8') as f_out:
            f_out.write(final_dat_content)
        end_time = time.time()
        # Riconta i giochi nel file finale per un report più accurato
        final_game_count = 0
        try:
             final_tree = ET.fromstring(final_dat_content.encode('utf-8'))
             final_game_count = len(final_tree.findall('.//game'))
        except Exception: # Fallback se il parsing del file finale fallisce
             final_game_count = final_dat_content.count("<game name=") # Conteggio approssimativo

        console.print(Panel(
            f"File originale: [cyan]{filepath.name}[/cyan] ({game_count_original} giochi)\n"
            f"File filtrato: [green]{safe_output_filename}[/green] ({final_game_count} giochi)\n"
            f"Tempo impiegato: [yellow]{end_time - start_time:.2f}s[/yellow]",
            title="[bold green]Salvataggio Completato[/bold green]",
            border_style="green"
        ))
        console.rule(style="green")
        return True
    except IOError as e:
        console.print(f"[red]Errore I/O durante salvataggio {safe_output_filename}:[/red] {e}", style="bold red")
    except Exception as e:
        console.print(f"[red]Errore inaspettato durante salvataggio {safe_output_filename}:[/red] {e}", style="bold red")

    console.rule(style="red")
    return False


# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"Gemini AKNF Roms Filter Script v{SCRIPT_VERSION}")
    parser.add_argument("--all", action="store_true",
                        help="Processa tutti i file .dat nella directory dello script (esclusi quelli già filtrati).")
    parser.add_argument("filename", nargs='?', type=Path,
                        help="Percorso del file .dat specifico da processare.")
    parser.add_argument("-s", "--score_threshold", type=int, default=75,
                        help="Soglia minima del punteggio stimato per la critica (default: 75). Valori più bassi includeranno più giochi.")

    args = parser.parse_args()

    if not RICH_AVAILABLE:
        print(f"Gemini AKNF Roms Filter Script v{SCRIPT_VERSION}")
        print("Avviso: Libreria 'rich' non trovata. L'output non sarà colorato.")
        print("Installala con: pip install rich")
    if not LXML_AVAILABLE:
         print("Avviso: Libreria 'lxml' non trovata. Si raccomanda 'pip install lxml'.")


    console.print(Panel(f"Gemini AKNF Roms Filter Script [bold]v{SCRIPT_VERSION}[/bold] (Modello: {GEMINI_MODEL_NAME})",
                        title="[bold blue]Avvio Script[/bold blue]", border_style="blue"))

    success_count = 0
    fail_count = 0
    total_to_process = 0
    files_to_process = []

    if args.filename:
        file_path = args.filename.resolve()
        if file_path.is_file() and file_path.suffix.lower() == '.dat':
            files_to_process.append(file_path)
            total_to_process = 1
        else:
            console.print(f"[red]Errore:[/red] File specificato '{args.filename}' non valido o non trovato.", style="bold red")
            sys.exit(1)
    elif args.all:
        script_dir = Path(__file__).parent.resolve()
        console.print(f"Ricerca file .dat in: [cyan]{script_dir}[/cyan]")
        all_dats = list(script_dir.glob('*.dat'))
        files_to_process = [
            f for f in all_dats
            if "(GeminiAKNF" not in f.name and
               not f.name.endswith(("_compressed.xml", "_ai_response.xml",
                                    "failed_ai_response_games.xml",
                                    "invalid_ai_response_start.xml",
                                    "failed_ai_response.xml"))
        ]
        total_to_process = len(files_to_process)
        if not files_to_process:
            console.print("[yellow]Nessun file .dat valido trovato da processare nella directory.[/yellow]")
        else:
            console.print(f"Trovati [bold cyan]{total_to_process}[/bold cyan] file .dat da processare:")
            files_to_process.sort(key=lambda p: p.name)
            for dat_file in files_to_process:
                console.print(f"- [dim]{dat_file.name}[/dim]")
    else:
        console.print("[red]Errore:[/red] Specificare un percorso file o usare l'opzione --all.", style="bold red")
        parser.print_help()
        sys.exit(1)

    if files_to_process:
        console.rule(f"Inizio Elaborazione File (Soglia Punteggio: {args.score_threshold})", style="blue")
        for i, dat_file in enumerate(files_to_process):
            # Passa la soglia letta dagli argomenti
            if process_dat_file(dat_file, args.score_threshold):
                success_count += 1
            else:
                fail_count += 1
            if total_to_process > 1 and i < total_to_process - 1:
                console.print("\n[dim]Pausa breve prima del prossimo file...[/dim]")
                time.sleep(1) # Pausa breve

    summary_style = "green" if fail_count == 0 and total_to_process > 0 else ("yellow" if success_count > 0 else "red")
    fail_color = "red" if fail_count > 0 else "white"

    console.print(Panel(
        f"File totali tentati: {total_to_process}\n"
        f"  [green]Successo:[/green] {success_count}\n"
        f"  [{fail_color}]Falliti:[/{fail_color}] {fail_count}",
        title="[bold blue]Riepilogo Elaborazione[/bold blue]",
        border_style=summary_style
    ))
