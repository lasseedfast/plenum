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
									<Cell key={party} fill={meta?.parties?.[party] ?? "#999"} />
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
