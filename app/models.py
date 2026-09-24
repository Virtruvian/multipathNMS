from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss_percent: Mapped[float] = mapped_column(Float, default=0.0)
    jitter_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    samples: Mapped[list["Sample"]] = relationship(back_populates="target", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(back_populates="target", cascade="all, delete-orphan")
    topology_snapshots: Mapped[list["TopologySnapshot"]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )
    topology_nodes: Mapped[list["TopologyNode"]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )
    topology_edges: Mapped[list["TopologyEdge"]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )
    route_paths: Mapped[list["RoutePath"]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    success: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    target: Mapped[Target] = relationship(back_populates="samples")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    event_type: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)

    target: Mapped[Target | None] = relationship(back_populates="events")


class TopologySnapshot(Base):
    __tablename__ = "topology_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_ip: Mapped[str] = mapped_column(String(64))
    probe_count: Mapped[int] = mapped_column(Integer, default=0)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    edge_count: Mapped[int] = mapped_column(Integer, default=0)
    route_count: Mapped[int] = mapped_column(Integer, default=0)

    target: Mapped[Target] = relationship(back_populates="topology_snapshots")


class TopologyNode(Base):
    __tablename__ = "topology_nodes"
    __table_args__ = (UniqueConstraint("target_id", "ttl", "address", name="uq_topology_node"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    ttl: Mapped[int] = mapped_column(Integer)
    address: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    target: Mapped[Target] = relationship(back_populates="topology_nodes")


class TopologyEdge(Base):
    __tablename__ = "topology_edges"
    __table_args__ = (
        UniqueConstraint(
            "target_id", "source_node_id", "destination_node_id", name="uq_topology_edge"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    source_node_id: Mapped[int] = mapped_column(ForeignKey("topology_nodes.id"), index=True)
    destination_node_id: Mapped[int] = mapped_column(ForeignKey("topology_nodes.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    target: Mapped[Target] = relationship(back_populates="topology_edges")


class RoutePath(Base):
    __tablename__ = "route_paths"
    __table_args__ = (UniqueConstraint("target_id", "path_hash", name="uq_route_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    path_hash: Mapped[str] = mapped_column(String(40), index=True)
    route_index: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    hop_count: Mapped[int] = mapped_column(Integer, default=0)
    flow_count: Mapped[int] = mapped_column(Integer, default=0)
    destination_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    minimum_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    seen_count: Mapped[int] = mapped_column(Integer, default=0)
    miss_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_change: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    target: Mapped[Target] = relationship(back_populates="route_paths")
    hops: Mapped[list["RouteHop"]] = relationship(
        back_populates="route_path",
        cascade="all, delete-orphan",
        order_by="RouteHop.ttl",
    )


class RouteHop(Base):
    __tablename__ = "route_hops"
    __table_args__ = (UniqueConstraint("route_path_id", "ttl", name="uq_route_hop"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_path_id: Mapped[int] = mapped_column(ForeignKey("route_paths.id"), index=True)
    ttl: Mapped[int] = mapped_column(Integer)
    address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    samples: Mapped[int] = mapped_column(Integer, default=0)

    route_path: Mapped[RoutePath] = relationship(back_populates="hops")
