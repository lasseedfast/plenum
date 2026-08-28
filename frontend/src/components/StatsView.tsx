import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, BarChart, XAxis, YAxis, Bar } from "recharts";
import type { AggregatedStats, MetaResponse } from "../types";

type Props = {
	stats: AggregatedStats;
	meta?: MetaResponse;
};

export function StatsView({ stats, meta }: Props) {
	const partyEntries = Object.entries(stats.per_party);
	const yearEntries = Object.entries(stats.per_year)
		.map(([year, value]) => ({ year, value }))
		.sort((a, b) => Number(a.year) - Number(b.year));

	// Fold the case: the codes arrive from a SQL GROUP BY, and a slice whose colour
	// depends on how the database happened to spell it is a bug that raises nothing.
	// Older motion rows held `c` next to `C`, and the symptom was a slice rendering in
	// the unknown-party grey. That data is normalised now; this keeps it from mattering
	// again.
	const partyColor = (code: string) =>
		meta?.parties?.find((p) => p.code.toUpperCase() === code.toUpperCase())?.color ??
		meta?.party_defaults?.unknown_color ??
		"#999";

	return (
		<section className="stats-view panel">
			<header>
				<h2>Träffar: {stats.total}</h2>
			</header>
			<div className="charts">
				<div className="chart">
					<h3>Partifördelning</h3>
					<ResponsiveContainer height={280}>
						<PieChart>
							<Pie data={partyEntries} dataKey={1} nameKey={0} innerRadius={60} outerRadius={100}>
								{partyEntries.map(([party]) => (
									<Cell key={party} fill={partyColor(party)} />
								))}
							</Pie>
							<Tooltip />
						</PieChart>
					</ResponsiveContainer>
				</div>
				<div className="chart">
					<h3>År</h3>
					<ResponsiveContainer height={280}>
						<BarChart data={yearEntries}>
							<XAxis dataKey="year" />
							<YAxis allowDecimals={false} />
							<Tooltip />
							<Bar dataKey="value" fill="#3f51b5" />
						</BarChart>
					</ResponsiveContainer>
				</div>
			</div>
		</section>
	);
}
