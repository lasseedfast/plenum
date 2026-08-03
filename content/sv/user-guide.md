# Riksdagen – transparensguide

Den här guiden förklarar hur Riksdagen-appen är byggd, vilka val som gjorts, vad som ingår i databasen och hur man ska tolka svaren. Den är skriven för att kunna kopieras in i ett AI-samtal, så att en AI-assistent kan hjälpa till att besvara frågor om appen och dess begränsningar.

---

## Om rixdagen.se

Sajten är utvecklad av [Lasse Edfast](https://lasseedfast.se), hittills utan ekonomiskt stöd. Den är gratis att använda och ingen information sparas utöver de sju dagarna som en chatt sparas i. 

Vill du vara med och utveckla, eller bara har frågor, kontakta gärna Lasse via [e-post](mailto:lasse@edfast.se).

---

## Vad appen gör

Appen har två lägen som kompletterar varandra: **Sök** och **Chat**.

**Sök** är en direktsökning i riksdagstalen. Du skriver ord eller fraser, sätter eventuellt filter på parti, debatttyp och år, och får träffar rankade efter relevans. Söket är deterministiskt och transparent: du ser exakt vilka tal som matchade, med utdrag ur originaltexten. Det är bra för att bläddra, verifiera och exportera specifikt material.

**Chat** är en AI-agent som aktivt planerar sin sökning, kör flera databassökningar, resonerar kring resultaten och skriver ett sammanhållande svar med källhänvisningar. Det är bra för analytiska frågor, jämförelser över tid eller mellan partier, och frågor som kräver att man sätter samman information från många tal.

De två lägena når samma underliggande databas men på olika sätt. Läs nedan för att förstå hur.

---

## Datakällan

All data hämtas från Riksdagens öppna API på `data.riksdagen.se`. Det är riksdagens egna data – samma data som riksdagen offentliggör. Appen lägger inte till eller tolkar politiskt innehåll utöver vad som beskrivs nedan.

### Vad som ingår

- **Anföranden (tal)** från riksdagens kammare, från riksdagsåret 1993/200
 till och med 2026. Det är ungefär 450 000 tal totalt.
- Varje tal innehåller bl.a: talarens namn, parti, datum, hela taltexten, debatttyp (t.ex. interpellationsdebatt, budgetdebatt) och om det är ett replikanförande.

### Vad som inte ingår

- **Inga röstningsdata.** Hur riksdagsledamöter röstat i sakfrågor finns inte i appen.
- **Inga motioner, betänkanden eller skriftliga frågor** i sin helhet. Bara kammarens muntliga anföranden.
- **Inget material från perioder före 1993.** Data börjar med riksdagsåret 1993/1994.
- Informellt partiinternt material, pressmeddelanden, bloggar eller annat utanför riksdagens officiella data.

---

## Hur data har bearbetats

Rådata från riksdagen har bearbetats i flera steg för att göra sökning och AI-svar möjliga. Dessa påverkar inte vad som finns i övrigt och chatten har hela tiden tillgång till hela texten, utan sammanfattningarna är bara ett extra lager av metadata som AI:n kan använda för att navigera i materialet och som användaren kan använda för att snabbt förstå vad ett tal handlar om.

### 1. AI-genererade sammanfattningar

Varje tal längre än 200 tecken har sammanfattats av en AI i tre steg:

1. **Sammanfattning** – 2–4 meningar som beskriver vad talaren säger.
2. **Argumentlista** – en strukturerad lista med de politiska argument som framförs.
3. **Ämnestaggar** – 1–3 taggar valda ur ett kontrollerat vokabulär på 38 ämnesområden (t.ex. "sjukvård", "skatter", "migration").

För replikanföranden har AI:n fått kontext från det inledande anförandet och närmast föregående inlägg, för att kunna förstå sammanhanget talaren talar i.

**Viktig begränsning:** Sammanfattningarna är AI-genererade och kan missa nyanser, kondensera argument felaktigt eller förenkla komplicerade ståndpunkter. De är hjälpmedel, inte auktoritativa texter. Källhänvisningarna i appen pekar till originaltalen.

Tal kortare än 200 tecken (korta procedurella inlägg, rop från golvet) har inte sammanfattats eller taggats alls. De kan hittas med textsökning men inte med semantisk sökning baserad på sammanfattningar.

### 2. Vektorer för semantisk sökning

Varje tal har också konverterats till numeriska vektorer (embeddings) som representerar textens semantiska innehåll. Det finns tre typer:

- **Chunk-embeddings:** taltexten delas upp i avsnitt om ca 500 tecken. Varje avsnitt får en vektor. Ger exakta träffar på specifika passager.
- **Sammanfattningsembeddings:** AI-sammanfattningen av varje tal omvandlas till en vektor. Ger tematiska träffar – hittar tal som handlar om ett ämne även om exakta ord saknas.
- **Debattembeddings:** varje debattsession (en rad av tal som är replikanföranden) får en vektoriserad sammanfattning. Används för att navigera till rätt debatt.

Modellen som genererar vektorerna är `qwen3-embedding` (0.6B parametrar), körd lokalt. Vektorerna lagras i PostgreSQL med pgvector och indexeras med HNSW för snabb semantisk sökning.

### 3. Fulltextindex

Hela taltexten är indexerat för textsökning med svensk ordstamsanalys (PostgreSQL, konfiguration "swedish"). Det innebär att sökning på "sjukvård" också hittar "sjukvården", "sjukvårdens" osv.

---

## Söket – direkt textsökning

Söket är en direktsökning mot riksdagstalen utan AI-inblandning. Det söker i hela taltexten (inte bara sammanfattningar) och rankar träffar efter textrelevans. För ranking används PostgreSQL:s `ts_rank_cd` för att bestämma relevans, se nedan.

### Vad söket stöder

- **Fritext:** vanliga ord och fraser rankas med svensk ordstamsanalys, så "sjukvård" matchar "sjukvården", "sjukvårdens" osv.
- **Exakta fraser:** använd citattecken, t.ex. `"fossilfri fordonsflotta"`, för att kräva att orden förekommer i den exakta ordningen.
- **Booleska operatorer:** `OR` mellan ord hittar tal som innehåller något av dem (`vindkraft OR kärnkraft`). Ett minustecken utesluter ett ord (`klimat -skatt`).
- **Namnsökning:** skriv `@` följt av ett namn för att söka vad en specifik talare sagt (`@Anna Kinberg Batra skatter`). Autokomplettering föreslår namn ur databasen.
- **Filter:** parti (ett eller flera), debatttyp (t.ex. interpellationsdebatt, budgetdebatt), och år via ett dubbelsidat skjutreglage.

### Vad söket returnerar

Varje träff visar datum, talare (klickbar länk till ledamotsprofilen), parti (färgkodad), debatttyp och ett utdrag ur originaltexten kring det sökta begreppet. Utdraget kan expanderas för att visa mer kontext. Ranking sker med PostgreSQL:s `ts_rank_cd`, som tar hänsyn till hur ofta och hur centralt sökorden förekommer i texten.

Ovanför resultatlistan visas två diagram: partifördelning (pajdiagram) och årsfördelning (stapeldiagram) för hela träffmängden – inte bara de 25 som visas på sidan. Diagrammen ger en snabb bild av vem som drivit en fråga och när.

Resultaten kan sorteras på relevans eller datum, och hela träffmängden kan laddas ner som CSV för vidare analys.

### Styrkor och begränsningar

Söket är transparent och kontrollerbart: du ser exakt vilka tal matchade och varför. Det är lämpat för att verifiera påståenden, hämta specifika utdrag, göra kvantitativa jämförelser och exportera underlag för vidare analys.

Söket hittar inte tematiskt liknande tal om de saknar rätt nyckelord. Om ett begrepp har bytts ut under perioden (t.ex. "invandrare" vs. "nyanlända") krävs separata sökningar. För den typen av tvärsökning är chatten bättre lämpad.

---

## Chatten – AI-agent

När du ställer en fråga i chatten händer följande:

### Steg 1: Planering

En språkmodell läser din fråga och bryter ner den i 1–3 **delfrågor** som var och en kan undersökas självständigt. Resultatet är ett strukturerat researchschema (Pydantic-modellen `ResearchRequest`) som styr nästa steg.

- Är frågan enkel/atomär — t.ex. "Hur många gånger har Annie Lööf talat om migration?" — returneras EN delfråga och planeringen fungerar mest som en sanity-check. Då hoppar agenten över forskarsteget och går direkt till orkestratorn (steg 3).
- Har frågan flera tydliga delar — t.ex. "Jämför hur S och M argumenterat om elpriser respektive räntor sedan 2020" — bryts den ner i 2–3 delfrågor och en specialiserad **Forskare** dispatcheras.

### Steg 2: Forskaren (vid 2+ delfrågor)

För varje delfråga kör en separat instans av modellen en **avgränsad sökrunda** (max 5 verktygsanrop per delfråga). Forskaren har samma sökverktyg som orkestratorn men ett enklare uppdrag: undersök *en* delfråga, samla källor, returnera en strukturerad **SubFinding** med:

- 1–3 meningars svar på svenska,
- en lista av käll-id:n (max 8) som faktiskt använts,
- en confidence-bedömning (`high` / `medium` / `low`),
- eventuella luckor som inte kunde besvaras.

Forskaren skickar **`status`-events** löpande så användaren ser vilken delfråga som undersöks just nu. Shadow communicator-tråden (se nedan) körs även här, så fynd-kort dyker upp under researchfasen — viktigt för längre frågor som kan ta flera minuter.

När alla delfrågor är klara samlas resultaten i en **ResearchReport** som injiceras i orkestratorns minne som ett kompakt sammandrag. Orkestratorn ser alltså aldrig de råa sökresultaten — bara forskarens sammanfattning plus käll-id:n.

### Steg 3: Sökning med verktyg

Orkestratorn (huvudmodellen) tar vid och har tillgång till sex verktyg:

1. **Textsökning** – nyckelordsbaserad sökning med stöd för booleska operatorer (AND/OR/NOT), exakta fraser, prefixsökning (ord*), och filter på parti, talare och år.
2. **Semantisk sökning** – söker i vektorerna och hittar tal som liknar frågan i mening, även om exakta ord saknas. Kombinerar chunk-träffar och sammanfattningsträffar.
3. **Debattsökning** – hittar relevanta debattsessioner via debattsammanfattningar.
4. **Debatthämtning** – läser in en hel debatt och rangordnar talen efter relevans för frågan.
5. **Direkta databasförfrågningar** – SQL-frågor för statistik, räkningar och aggregeringar.
6. **Källuppslag (`lookup_source`)** – återhämtar lagrad grundtext för käll-id:n agenten redan sett. Används när ett påstående behöver verifieras eller citeras ordagrant.

Orkestratorn kör upp till 20 sökrundor. Komplexa frågor kan kräva flera steg: hitta relevanta debatter → hämta specifika tal → ställa statistikfrågor för att komplettera. Om 20 rundor inte räcker till kan svaret bli ofullständigt.

### Steg 4: Kontexthantering (eviction + provenance)

För att orkestratorn inte ska drunkna i råtext (vilket annars kan spränga modellens kontextfönster på 22 000 tokens) använder appen ett **eviction-system**:

- Varje sökträff registreras i en **ProvenanceRegistry** med fullständig grundtext (capped vid 3 000 tecken per källa).
- I orkestratorns synliga historik byts den råa texten ut mot en kompakt rad: `[src:ID] Talare (Parti) datum — rubrik — kort förhandsvisning`.
- Behöver orkestratorn ordagrann text för att citera eller verifiera — anropar den `lookup_source([...])` med upp till 5 id:n åt gången (max 1 500 tecken per källa).
- Om hela meddelandehistoriken trots detta växer förbi en mjuk gräns (~50 000 tecken) komprimeras de äldsta verktygsresultaten ytterligare till en placeholder. Källorna lever kvar i registret och kan alltid återhämtas.

Långa resultat (>10 000 tecken) kan dessutom köras genom en snabbare AI-modell för per-anrops-komprimering. Källhänvisningstaggar (`[src:...]`) bevaras genom hela kedjan så att slutsvarets fotnoter alltid pekar till verkliga tal.

### Steg 5: Svar med källhänvisningar

Svaret skrivs alltid på svenska, oavsett vilket språk frågan ställs på. Svaret innehåller inline-källhänvisningar som superskript (`¹`, `²` osv.) klickbara direkt till originaltalet. Politikernamn som förekommer i svaret länkas automatiskt till ledamotsprofilen om namnet är unikt i databasen. Källorna listas även separat under svaret med talare och datum.

Medan agenten söker kan insikter dyka upp i realtid som separata kort – det är "shadow communicator"-funktionen som löpande surfar fynd utan att invänta det färdiga svaret. Den körs både under forskarsteget och under orkestratorns slutfas, med en delad dedup-lista så att samma insikt inte upprepas.

Endast källhänvisningar som faktiskt härstammar från databasens sökresultat kan visas – ett separat valideringssteg filtrerar bort eventuella påhittade ID:n. Om modellen citerar enbart ogiltiga ID:n får den en chans att söka om och bygga svaret med riktiga källor (max två omförsök).

**Viktigt:** Valideringen skyddar mot fabricerade käll-ID:n, men inte mot att fel tal kopplas till ett korrekt ID. Kontrollera alltid viktiga påståenden mot källanförandet.

### Snabbsvar — hoppa över forskaren

För frågor där du vill ha ett snabbt svar utan djup research finns ett **`quick`-läge**. När det är aktivt skippas planeringssteget och forskaren helt – orkestratorn svarar direkt med sina vanliga sökverktyg. Det sparar typiskt en LLM-omgång och är lämpligt för enkla faktafrågor eller när du redan vet att en runda räcker. Som standard är `quick=false`, dvs. djup research är på.

### Dela och spara chattar

Varje chattkonversation har en unik URL. Konversationen sparas i sju dagar från senaste aktivitet. Via "Dela konversation" skapas en kopia av konversationen som andra kan öppna och fortsätta utan att påverka originalet. Delningslänken upphör inte att gälla men den kopierade chatten försvinner efter sju dagars inaktivitet.

---

## Modellen som driver appen

Som standard drivs appen av en självhostad AI-modell  (just nu `qwen3.5-9b`), servad via vLLM på en lokal server. Det är en open-source-modell.

**Vad det innebär i praktiken:**

- Modellen är avsevärt mindre och svagare än t.ex. GPT-5, Claude 3.5 Sonnet eller Gemini 1.5 Pro. Den ger rimliga svar på väldefinierade frågor men hanterar komplex flerstegslogik sämre.
- Modellen instrueras att lita på databasens data framför sin egen träningskunskap när det gäller faktapåståenden om vad som sagts i Riksdagen, och instruktionerna är tydliga med att den inte ska gissa eller hitta på information. Det minskar risken för hallucinationer, men det kan fortfarande hända att modellen formulerar sig på ett sätt som låter trovärdigt utan att vara exakt. Det är särskilt viktigt att dubbelkolla källhänvisningarna i svaret.
- Modellen svarar på svenska men formulerar sig inte alltid korrekt vad gäller grammatik, stavning eller meningsbyggnad. På samma sätt kan den även missförstå frågans avsikt eller nyanser.
- Kapaciteten att köra på en liten modell är ett medvetet val som möjliggör lokal drift utan beroende av externa (och ofta dyra) AI-tjänster. Nackdelen är lägre svarskvalitet jämfört med vad en kommersiell modell ger.

---

## Välj AI-leverantör (eget API-konto)

Om du vill använda en starkare modell kan du koppla in ett eget konto hos en extern AI-leverantör. Inställningen finns under knappen **LLM-inställningar** i chattläget.

### Hur det fungerar

1. Välj en leverantör i listan (t.ex. Berget AI, OpenRouter, OpenAI).
2. Ange din API-nyckel i fältet som dyker upp.
3. Klicka **Hämta modeller** – appen frågar leverantören vilka modeller du har tillgång till och listar bara de som stöder verktygssökning (som chatten kräver).
4. Välj modell. Om du vill kan du också välja en separat, snabbare modell för sammanfattningssteget.

Chatten använder sedan din nyckel och modell för resten av sessionen. Serverns egna modell används alltså inte alls när en override är aktiv.

### Vad är en API-nyckel?

En API-nyckel är ett lösenord som identifierar dig mot en AI-leverantörs server. Du skapar den i ditt konto hos respektive leverantör:

- **Berget AI** – [berget.ai](https://berget.ai) (nordisk leverantör, data stannar i Norden)
- **OpenRouter** – [openrouter.ai](https://openrouter.ai) (aggregator med tillgång till hundratals modeller)
- **OpenAI** – [platform.openai.com](https://platform.openai.com) (samma modeller som driver ChatGPT)

Användning av externa modeller kostar pengar och debiteras direkt från ditt konto hos leverantören – inte via rixdagen.se.

### Hur nyckeln lagras

Din nyckel sparas **enbart lokalt i din webbläsare** (`localStorage`). Den skickas till rixdagen.se:s server bara för att vidarebefordras till leverantören när ett anrop görs – den loggas inte, sparas inte i databasen och syns aldrig i någon annan användares session.

Konkret:
- Nyckeln ligger kvar i webbläsaren tills du rensar den (knappen ✕ i inställningspanelen) eller tömmer webbläsarens lagringsdata.
- Den skickas med varje chattbegäran i krypterad form (HTTPS).
- Servern läser nyckeln, använder den för det enskilda anropet och håller den aldrig i minnet längre än nödvändigt.
- Att öppna appen i ett privat fönster innebär att nyckeln inte sparas alls – den försvinner när fliken stängs.

### Byta tillbaka till standardmodellen

Välj **Standardmodell (serverns egen)** längst upp i providerlistan – chatten återgår direkt till serverns egna modell. Nyckeln för den externa providern är fortfarande sparad i webbläsaren om du vill byta tillbaka senare.

---

## Kända begränsningar

### Täckning och data
- **Ingen data före 1993.** Frågor om riksdagsdebatter från 1980-tal eller tidigt 1990-tal kan inte besvaras.
- **Inga röster.** Det går inte att ta reda på hur en specifik ledamot röstat i en specifik votering.
- **Inga motioner eller betänkanden.** Bara muntliga anföranden i kammaren.
- **Korta inlägg saknar semantisk indexering.** Procedurella yttranden under 200 tecken har inga AI-sammanfattningar och hittas inte via semantisk sökning.

### Sökning och precision
- **Semantisk sökning är ungefärlig.** Den hittar tematiskt liknande innehåll men missar ibland relevanta tal och inkluderar ibland irrelevanta. Förlita dig inte enbart på semantisk sökning för heltäckande undersökningar.
- **Textsökning kräver rätt ord.** Om ett begrepp har bytts ut eller stavats annorlunda i riksdagen kan relevanta tal missas.
- **Debattembeddings är ofullständiga.** Äldre debatter kan sakna vektoriserade sammanfattningar.

### AI och tillförlitlighet
- **Sammanfattningar är approximationer.** AI-sammanfattningarna kondenserar innehållet och kan missa viktiga nyanser eller formuleringar.
- **Agenten kan missnöja med verktygsval.** Ibland väljer agenten fel sökstrategi eller missar att kombinera sökningar på bästa sätt.
- **Komprimering tappar information.** Långa sökresultat komprimeras innan de når huvudmodellen – information kan falla bort. Eviction-systemet (steg 4) kompenserar genom att hålla originaltexten tillgänglig via `lookup_source`, men om agenten missar att hämta tillbaka rätt källa kan ett påstående bli sämre underbyggt än det borde.
- **Forskaren kan missa något.** Vid 2+ delfrågor delegeras research till en separat AI-instans som returnerar en sammanfattning. Orkestratorn ser bara sammanfattningen, inte de råa träffarna. Det håller kontextfönstret hanterbart men innebär att eventuella missförstånd hos forskaren kan följa med in i slutsvaret.

### Vad man bör dubbelkolla
- Direkta citat: appen citerar vanligtvis inte ordagrant (och ska inte göra det), men om ett citatliknande påstående förekommer – kontrollera källan.
- Statistiska påståenden (t.ex. "SD nämnde migration 847 gånger 2022") baseras på databasens data, men sökfrågan kan ha avgränsats på ett sätt som ger ett ofullständigt tal.
- Påståenden om vad en person "inte sagt" är alltid svåra att verifiera i en sökbaserad app.

---

## Vad appen är bra på

### Söket passar bra för
- Hitta alla tal av en specifik person om ett specifikt ämne
- Verifiera om ett ord, en fras eller ett specifikt påstående förekommer i riksdagsdebatter
- Bläddra i material från ett specifikt år, parti eller debatttyp
- Hämta underlag för vidare läsning i originalkällor

### Chatten passar bra för
- *Vad har [parti/person] sagt om [ämne] de senaste åren?*
- *Hur har debatten om [ämne] förändrats över tid?*
- *Vilket parti har pratat mest om [ämne]?*
- *Vilka argument framförde oppositionen i debatten om [specifik reform] år [X]?*
- *Finns det riksdagsledamöter som konsekvent tagit en avvikande ståndpunkt om [ämne]?*
- *Hur många anföranden handlade om [ämne] per år?*

## Frågor appen inte kan besvara

- *Hur röstade [parti] om [lagstiftning]?* – Röstdata ingår inte.
- *Vad innehåller motion [nummer]?* – Motioner ingår inte.
- *Vad hände i Riksdagen under 1980-talet?* – Data börjar 1993.
- *Är det sant att [faktapåstående om omvärlden]?* – Modellen ska inte användas för allmän faktakoll; den är optimerad för riksdagsdata.

---

## Hur man värderar ett svar

1. **Kolla källhänvisningarna.** Varje påstående i svaret bör ha en källa. Klicka på källan och läs originalet. Appen länkar direkt till riksdagens öppna data.

2. **Fundera på söktäckning.** Fick agenten svar på sin sökning? Ibland framgår det i svarets formulering att sökningen gett begränsat material. Det kan bero på att lite sagts om ämnet – eller att sökningen var dåligt formulerad.

3. **Var försiktig med negativa påståenden.** "Inget parti har tagit upp X" är svårt att belägga med sökning. Det kan lika gärna bero på ett sökmiss.

4. **Statistik är beroende av sökfrågan.** Siffror som "47 anföranden om X" är beroende av exakt hur sökningen avgränsats. Små förändringar i ordval kan ge andra siffror.

5. **Läs gärna ursprungstalet.** Sammanfattningar och agentens parafraseringar är alltid approximationer av originalet. SVT:s riksdagssändning länkas direkt från varje tal.


---
## Teknisk sammanfattning (för AI-assistenter)

- **Databas:** PostgreSQL med pgvector. Tabeller: `talks`, `chunks`, `debates`, `people`.
- **Datakälla:** Riksdagens öppna API, anförandedatasetet, 1993–2026, ~450 000 tal.
- **Embeddingmodell:** `qwen3-embedding` (0.6B), 384 dimensioner, lokal Ollama-instans.
- **Vektordimensioner:** 384.
- **Embeddingindex:** HNSW i pgvector, kosinuslikhet.
- **Textindex:** PostgreSQL `TSVECTOR` med `swedish`-konfiguration, GIN-index, `ts_rank_cd`-ranking.
- **LLM-agent:** Standardmodell: `gpt-oss:20b` (lokal vLLM). Kan åsidosättas per session med användarsupplied API-nyckel mot Berget AI, OpenRouter eller OpenAI – konfigureras i `providers.yaml`. Nyckeln lagras aldrig på servern.
- **Provider-override:** `provider_id`, `api_key`, `smart_model` och `fast_model` skickas med varje chattbegäran; servern bygger ephemera LLM-instanser per request.
- **Tankeläge:** Aktivt på första iterationen, inaktiverat på efterföljande för latensoptimering.
- **Planerare/Forskare:** `_plan_research` returnerar en `ResearchRequest` med 1–3 `SubQuestion`. Vid ≥2 subfrågor kör `_run_researcher` en avgränsad sökrunda per subfråga (max 5 verktygsanrop) och returnerar en `ResearchReport` (`SubFinding[]`) som injiceras i orkestratorns historik.
- **Quick-läge:** `quick=true` i `ChatRequest` skippar både planerare och forskare; orkestratorn svarar direkt.
- **Sökverktyg:** `search_speeches` (FTS), `vector_search` (hybrid chunk+summary), `vector_search_debates`, `fetch_debate`, `database_query`, `fetch_speeches`, `lookup_source` (registry-uppslag).
- **Källvalidering:** `ProvenanceRegistry` mappar varje `[src:ID]` mot verkliga sökträffar; ogiltiga ID:n filtreras bort. Vid 100 % ogiltiga citat tvingas modellen söka om (max 2 omförsök).
- **Provenance-grounding:** Varje registrerad källa lagras med fullständig grundtext (cap 3 000 tecken). `lookup_source` returnerar max 5 id per anrop, max 1 500 tecken per kropp.
- **Eviction:** Råa verktygssvar för sökverktyg byts ut mot en kompakt `[src:ID]`-stub i orkestratorns historik. `HISTORY_CHAR_BUDGET = 50 000` tecken; över gränsen komprimeras äldsta stubbar till en placeholder.
- **Shadow communicator:** Parallell LLM-tråd som observerar verktygsresultat och surfar fynd till användaren i realtid via SSE-events. Körs både i forskarens och orkestratorns sökrundor, med delad `sent_insights`-lista för dedup.
- **Komprimeringsgräns:** Verktygsresultat >10 000 tecken komprimeras av snabbmodellen innan de återmatas till orchestratorn.
- **Max iterationer per fråga:** 20 (orkestratorn). Vid iteration 19 tvingas ett slutsvar. Forskarens delfrågor: max 5 iterationer per delfråga.
- **AI-genererade fält per tal:** `summary` (2–4 meningar), `arguments` (JSON-array), `tags` (1–3 ur 38-kategoriersystem).
- **Minimitröskel för AI-bearbetning:** 200 tecken. Kortare tal saknar summary/arguments/tags.
- **Temperaturer:** Orchestrator 0.2, shadow communicator 0.3, komprimering 0.05, offline-pipelinen 0.2.
- **Kontextkomprimering:** Dokument >1 500 tecken sammanfattas individuellt (med prefix-caching via vLLM).
