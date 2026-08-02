import React from "react";

export function ExplainerText() {
	return (
		<details className="explainer">
			<summary className="explainer__summary">Hur fungerar söket?</summary>
			<div className="explainer__body">
				<p>
					Det här är en databas över vad svenska riksdagspolitiker har sagt i olika debatter sedan 1993.
					Datan kommer både från data.riksdagen.se och transkriberingar av Riksdagens videotjänst (från år 2000).
				</p>
				<p>
					Börja med att skriva ett eller flera sökord. Du kan använda asterisk (*), minus (-), citattecken (&quot;&quot;), OR
					och år:yyyy-yyyy. Exempel:
				</p>
				<p className="explainer__example">
					<code>energikris* baskraft OR kärnkraft &quot;fossilfria energikällor&quot; -vindkraft år:2015-2022</code>
				</p>
				<ul>
					<li>träffar ord som &quot;energikris&quot; och variationer som &quot;energikrisen&quot;</li>
					<li>kräver att &quot;baskraft&quot; <em>eller</em> &quot;kärnkraft&quot; nämns</li>
					<li>letar efter den exakta frasen &quot;fossilfria energikällor&quot;</li>
					<li>utesluter anföranden som nämner &quot;vindkraft&quot;</li>
					<li>begränsar träffarna till åren 2015–2022</li>
				</ul>
				<p>
					När du fått resultat kan du filtrera partier, justera år och välja debatttyper. Under &quot;Längre utdrag&quot; hittar
					du hela tal med länkar till Webb-TV och ljud när de finns.
				</p>
				<p>
					Har du idéer eller hittar buggar? <a href="mailto:lasse@edfast.se">Mejla mig</a> eller{" "}
					<a href="https://twitter.com/lasseedfast">skriv på Twitter</a>. / Lasse Edfast, journalist.
				</p>
			</div>
		</details>
	);
}