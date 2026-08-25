import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { suggestPeople } from "../api";
import { mpPath } from "../utils/mpLink";
import { PersonAvatar, describePerson } from "./PersonSuggestionList";

/** How many members to offer before it stops being a shortcut and becomes a list. */
const MAX_CARDS = 4;

/**
 * A name typed into the search box is usually a request for the person, not for
 * the word. Full-text search cannot answer that — searching "Löfven" returns
 * speeches where somebody said the name, never the man himself — so matching
 * members are offered above the results.
 *
 * Deliberately additive: the speech results still render below, because the
 * query may well have meant the word after all.
 */
export function PeopleResults({ query }: { query: string }) {
	const term = query.trim();
	// A person search is short. Anything longer is prose, and running it through
	// the name index only produces incidental substring hits.
	const looksLikeName = term.length >= 2 && term.length <= 40 && term.split(/\s+/).length <= 3;

	const { data: people = [] } = useQuery({
		queryKey: ["people-search", term],
		queryFn: () => suggestPeople(term, MAX_CARDS),
		enabled: looksLikeName,
		staleTime: 5 * 60 * 1000,
	});

	if (!looksLikeName || people.length === 0) return null;

	return (
		<section className="people-results panel" aria-label="Ledamöter som matchar sökningen">
			<h2 className="people-results__heading">Ledamöter</h2>
			<ul className="people-results__list">
				{people.map((person) => (
					<li key={person.person_id}>
						<Link to={mpPath(person.person_id, person.name)} className="people-results__card">
							<PersonAvatar person={person} className="people-results__avatar" />
							<span className="people-results__text">
								<span className="people-results__name-row">
									<span className="people-results__name">{person.name}</span>
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
								<span className="people-results__meta">{describePerson(person)}</span>
							</span>
						</Link>
					</li>
				))}
			</ul>
		</section>
	);
}
