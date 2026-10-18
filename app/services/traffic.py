"""Estimated car traffic congestion on Iași's major roads.

There is no live traffic feed. The estimate combines:
- the road itself (OpenStreetMap class, lanes per direction, traffic-signal density);
- city-wide demand by local hour and day type, from nitric oxide (NO) at RNMCA traffic station IS-1
  (Podu de Piatră). NO is emitted mainly by vehicle exhaust: hourly medians 2025-01-01 .. 2026-10-17
  (~15,700 hours), normalised so the weekday 07:00-08:00 peak is 1. Known bias: daytime atmospheric mixing
  dilutes NO, so the afternoon rush is somewhat underestimated and calm nights somewhat overestimated;
- live signals from the app: open community reports of category "traffic" and construction on the road.

Values are a relative index 0..1, not measured speeds.
"""

import math
from datetime import timedelta
from zoneinfo import ZoneInfo

from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

from app.services.scoring import projected

# Median NO at IS-1 per local hour (interval start), divided by the weekday maximum.
DEMAND = {
    "weekday": (
        0.23, 0.18, 0.19, 0.17, 0.25, 0.48, 0.81, 1.00, 0.93, 0.80, 0.66, 0.59,
        0.60, 0.57, 0.56, 0.56, 0.62, 0.62, 0.64, 0.59, 0.55, 0.52, 0.49, 0.31,
    ),
    "saturday": (
        0.26, 0.20, 0.19, 0.18, 0.25, 0.36, 0.46, 0.53, 0.59, 0.51, 0.48, 0.41,
        0.43, 0.37, 0.36, 0.35, 0.36, 0.39, 0.43, 0.44, 0.49, 0.50, 0.51, 0.38,
    ),
    "sunday": (
        0.28, 0.20, 0.18, 0.18, 0.19, 0.26, 0.34, 0.31, 0.34, 0.34, 0.30, 0.31,
        0.32, 0.35, 0.32, 0.31, 0.33, 0.37, 0.41, 0.47, 0.53, 0.49, 0.50, 0.40,
    ),
}  # fmt: skip
DEMAND_SOURCE = "NO la stația de trafic RNMCA IS-1, mediane orare 2025-2026"

# Calibrated so that at the weekday peak arteries with signals saturate, plain secondary roads are
# "aglomerat" and tertiary roads "moderat"; at midday most of the network is fluid or moderate.
CLASS_WEIGHT = {"motorway": 0.6, "trunk": 0.8, "primary": 0.75, "secondary": 0.55, "tertiary": 0.38}
LINK_FACTOR = 0.8
# Streets outside the major network (residential, service) carry little through traffic.
LOCAL_STREET_WEIGHT = 0.15

LEVELS = ((0.35, "fluid"), (0.55, "moderat"), (0.75, "aglomerat"), (math.inf, "foarte_aglomerat"))
LEVEL_RANK = {name: i for i, (_, name) in enumerate(LEVELS)}

REPORT_REACH_M = 100
CONSTRUCTION_REACH_M = 15
MATCH_DISTANCE_M = 15
MATCH_ANGLE_DEG = 35


def level(congestion):
    return next(name for limit, name in LEVELS if congestion < limit)


def day_type(local):
    return "weekday" if local.weekday() < 5 else "saturday" if local.weekday() == 5 else "sunday"


def demand_at(when, timezone="Europe/Bucharest"):
    """City-wide relative demand at `when`, interpolated between the hourly medians."""
    local = when.astimezone(ZoneInfo(timezone))
    start = local.replace(minute=0, second=0, microsecond=0)
    following = start + timedelta(hours=1)
    here = DEMAND[day_type(start)][start.hour]
    there = DEMAND[day_type(following)][following.hour]
    fraction = (local - start).total_seconds() / 3600
    return {"value": here + (there - here) * fraction, "day_type": day_type(local), "hour": local.hour}


def base_congestion(road, demand):
    """Congestion of one road from its attributes and the city-wide demand (0..1)."""
    kind = road.highway.removesuffix("_link")
    weight = CLASS_WEIGHT.get(kind, LOCAL_STREET_WEIGHT) * (
        LINK_FACTOR if road.highway.endswith("_link") else 1
    )
    # Short ways at junctions would get absurd densities: count at least 300 m.
    per_km = (road.signals or 0) / max(road.length_m / 1000, 0.3)
    signal_factor = 1 + min(0.35, 0.12 * per_km)
    lanes = road.lanes / (1 if road.oneway else 2) if road.lanes else None
    lane_factor = 1.0 if lanes is None else 1.1 if lanes <= 1 else 0.9 if lanes >= 2 else 1.0
    return min(1.0, weight * demand * signal_factor * lane_factor)


def time_factor(congestion):
    """Extra car travel time on a congested road: x1.2 at 0.5, x1.8 at 1.0 (free flow = 1)."""
    return 1 + 0.8 * congestion**2


class TrafficModel:
    """Congestion for every stored road at one moment, plus matching of route segments to roads."""

    def __init__(self, roads, demand, latitude, traffic_reports=(), construction=()):
        self.demand, self.latitude = demand, latitude
        self.roads, self.lines, self.values, self.reasons = [], [], [], []
        for road in roads:
            try:
                line = projected(shape(road.geometry), latitude)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if line.is_empty or line.length == 0:
                continue
            self.roads.append(road)
            self.lines.append(line)
            self.values.append(base_congestion(road, demand["value"]))
            self.reasons.append([])
        self.tree = STRtree(self.lines) if self.lines else None
        if self.tree is None:
            return
        for report in traffic_reports:
            point = projected(Point(report.longitude, report.latitude), latitude)
            for index in self.tree.query(point, predicate="dwithin", distance=REPORT_REACH_M):
                # A reported jam overrides the estimate: severity 1..5 maps to 0.6..1.0.
                floor = min(1.0, 0.5 + 0.1 * max(1, min(5, report.severity)))
                if floor > self.values[index]:
                    self.values[index] = floor
                self.reasons[index].append({"kind": "report", "id": report.id})
        for zone_id, polygon in construction:
            for index in self.tree.query(polygon, predicate="dwithin", distance=CONSTRUCTION_REACH_M):
                self.values[index] = min(1.0, self.values[index] + 0.15)
                self.reasons[index].append({"kind": "construction", "id": zone_id})

    def snapshot(self, minimum="moderat"):
        rank = LEVEL_RANK[minimum]
        items = []
        for road, value, reasons in zip(self.roads, self.values, self.reasons):
            name = level(value)
            if LEVEL_RANK[name] < rank:
                continue
            items.append(
                {
                    "id": road.id,
                    "name": road.name,
                    "highway": road.highway,
                    "congestion": round(value, 3),
                    "level": name,
                    "reasons": reasons,
                    "geometry": road.geometry,
                }
            )
        return items

    def at_segment(self, a, b):
        """Congestion where a route segment (projected points a -> b) runs along a major road, else None."""
        if self.tree is None:
            return None
        segment = LineString([a, b])
        if segment.length == 0:
            return None
        middle = segment.interpolate(0.5, normalized=True)
        best = None
        for index in self.tree.query(middle, predicate="dwithin", distance=MATCH_DISTANCE_M):
            line = self.lines[index]
            distance = line.distance(middle)
            # Crossing a road at a junction is not driving along it: directions must agree.
            along = line.project(middle)
            p, q = line.interpolate(max(0, along - 5)), line.interpolate(min(line.length, along + 5))
            if p.equals(q):
                continue
            road_angle = math.atan2(q.y - p.y, q.x - p.x)
            route_angle = math.atan2(b[1] - a[1], b[0] - a[0])
            diff = abs((road_angle - route_angle + math.pi) % (2 * math.pi) - math.pi)
            diff = min(diff, math.pi - diff)  # either travel direction
            if math.degrees(diff) > MATCH_ANGLE_DEG:
                continue
            if best is None or distance < best[0]:
                best = (distance, index)
        return self.values[best[1]] if best else None

    def local_congestion(self):
        return LOCAL_STREET_WEIGHT * self.demand["value"]
