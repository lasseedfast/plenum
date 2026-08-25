import type { PersonSuggestion } from "../types";

/**
 * The dropdown body shared by both mention inputs.
 *
 * A bare list of names cannot be picked from: two dozen members share a name
 * with someone else, and a name alone says nothing about whether this is the
 * sitting member or a backbencher who left in 1998. Photo, party, constituency
 * and speech count are what make the choice decidable.
 */
export function PersonSuggestionList({
	suggestions,
	activeIndex,
	onPick,
}: {
	suggestions: PersonSuggestion[];
	activeIndex: number;
	onPick: (suggestion: PersonSuggestion) => void;
}) {
	return (
		<ul className="person-suggest__list">
			{suggestions.map((person, index) => (
				<li
					key={person._key ?? person.person_id ?? person.name}
					role="option"
					aria-selected={index === activeIndex}
					className="person-suggest__item"
					data-active={index === activeIndex}
					// Keep focus in the input: a blur would close the popover
					// before the click lands.
					onMouseDown={(event) => event.preventDefault()}
					onClick={() => onPick(person)}
				>
					<PersonAvatar person={person} />
					<span className="person-suggest__text">
						<span className="person-suggest__name-row">
							<span className="person-suggest__name">{person.name}</span>
							{person.party && (
								<span
									className="party-chip party-chip--sm"
									data-party={person.party}
									style={{ "--party-color": `var(--party-${person.party})` } as React.CSSProperties}
								>
									{person.party}
								</span>
							)}
						</span>
						<span className="person-suggest__meta">{describePerson(person)}</span>
					</span>
				</li>
			))}
		</ul>
	);
}

/** Photo when the source has one, initials when it does not. */
export function PersonAvatar({ person, className }: { person: PersonSuggestion; className?: string }) {
	const classes = `person-suggest__avatar${className ? ` ${className}` : ""}`;
	if (!person.image_url) {
		return (
			<span className={`${classes} person-suggest__avatar--empty`} aria-hidden="true">
				{initials(person.name)}
			</span>
		);
	}
	return (
		<img
			className={classes}
			// Some imported photo URLs are http; the page is https.
			src={person.image_url.replace("http://", "https://")}
			alt=""
			loading="lazy"
			onError={(event) => {
				(event.currentTarget as HTMLImageElement).style.visibility = "hidden";
			}}
		/>
	);
}

function initials(name: string): string {
	return name
		.split(/\s+/)
		.filter(Boolean)
		.slice(0, 2)
		.map((part) => part[0]?.toUpperCase() ?? "")
		.join("");
}

/**
 * The second line: where they sit, and how present they are in the record.
 * "Tjänstgörande" is worth stating outright — it is the single fact that most
 * often separates the person being looked for from a namesake.
 */
export function describePerson(person: PersonSuggestion): string {
	const parts: string[] = [];
	if (person.constituency) parts.push(person.constituency);
	if (person.status === "Tjänstgörande riksdagsledamot") parts.push("Tjänstgörande");
	else if (person.last_speech) parts.push(`Senast ${person.last_speech.slice(0, 4)}`);
	if (person.speech_count) parts.push(`${person.speech_count.toLocaleString("sv-SE")} anföranden`);
	return parts.join(" · ");
}
