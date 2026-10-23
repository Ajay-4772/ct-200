from sqlalchemy import String, Integer, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    versions: Mapped[list['DocumentVersion']] = relationship(back_populates='document', cascade='all, delete-orphan')

class DocumentVersion(Base):
    __tablename__ = 'document_versions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey('documents.id'), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    source_filename: Mapped[str] = mapped_column(String)
    source_sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[Document] = relationship(back_populates='versions')
    nodes: Mapped[list['Node']] = relationship(back_populates='version', cascade='all, delete-orphan')
    __table_args__ = (UniqueConstraint('document_id','version_number'),)

class Node(Base):
    __tablename__ = 'nodes'
    id: Mapped[str] = mapped_column(String, primary_key=True) # version-specific ID
    logical_key: Mapped[str] = mapped_column(String, index=True) # cross-version identity
    version_id: Mapped[int] = mapped_column(ForeignKey('document_versions.id'), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey('nodes.id'), nullable=True, index=True)
    heading: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(Integer)
    section_number: Mapped[str] = mapped_column(String, default='')
    body_text: Mapped[str] = mapped_column(Text, default='')
    tables_json: Mapped[str] = mapped_column(Text, default='[]')
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    node_type: Mapped[str] = mapped_column(String, default='section')
    page_start: Mapped[int] = mapped_column(Integer, default=1)
    page_end: Mapped[int] = mapped_column(Integer, default=1)
    warnings_json: Mapped[str] = mapped_column(Text, default='[]')
    version: Mapped['DocumentVersion'] = relationship(back_populates='nodes', foreign_keys=[version_id])

class Selection(Base):
    __tablename__ = 'selections'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    document_version_id: Mapped[int] = mapped_column(ForeignKey('document_versions.id'))
    created_at: Mapped[str] = mapped_column(String)

class SelectionItem(Base):
    __tablename__ = 'selection_items'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    selection_id: Mapped[str] = mapped_column(ForeignKey('selections.id'), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey('nodes.id'))
    version_id: Mapped[int] = mapped_column(ForeignKey('document_versions.id'))
    source_text: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
