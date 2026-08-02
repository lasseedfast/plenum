import { useQuery } from "@tanstack/react-query";
import { useParams, useNavigate } from "react-router-dom";
import { fetchMotion } from "../api";
import { MotionBody } from "./MotionBody";
import type { Motion } from "../types";

/**
 * Standalone page for a single motion (route /motion/:id), the motion
 * counterpart to TalkView. Shares MotionBody with the drawer.
 */
export function MotionView() {
	const { id } = useParams<{ id: string }>();
	const navigate = useNavigate();

	const { data: motion, isLoading, error } = useQuery<Motion>({
		queryKey: ["motion", id],
		queryFn: () => fetchMotion(id!),
		enabled: !!id,
	});

	if (isLoading) {
		return <div className="talk-view"><div className="panel"><p>Laddar motion...</p></div></div>;
	}
	if (error) {
		return <div className="talk-view"><div className="panel error-banner">Kunde inte ladda motion: {(error as Error).message}</div></div>;
	}
	if (!motion) {
		return <div className="talk-view"><div className="panel"><p>Motion hittades inte.</p></div></div>;
	}

	const handleBackClick = () => {
		if (window.history.length > 1) navigate(-1);
		else navigate("/");
	};

	return (
		<div className="talk-view">
			<div className="talk-view__navRow">
				<div className="talk-view__navCell talk-view__navCell--center talk-view__navCell--minwidth">
					<button type="button" className="secondary-button talk-view__navButton" onClick={handleBackClick}>
						Tillbaka till sökresultat
					</button>
				</div>
			</div>
			<div className="panel">
				<MotionBody motion={motion} />
			</div>
		</div>
	);
}
