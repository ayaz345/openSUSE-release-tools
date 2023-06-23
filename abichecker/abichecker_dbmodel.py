#!/usr/bin/python3

import os
import sys
from datetime import datetime
from sqlalchemy import Column, ForeignKey, Integer, String, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, backref
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from abichecker_common import DATADIR

Base = declarative_base()

class Request(Base):
    __tablename__ = 'request'
    id = Column(Integer, primary_key=True)
    state = Column(String(32), nullable=False)
    result = Column(String(32), nullable=True)

    t_created = Column(DateTime, default=datetime.now)
    t_updated = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class Log(Base):
    __tablename__ = 'log'
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey('request.id'), nullable=False)
    request = relationship(Request, backref=backref('log', order_by=id, cascade="all, delete-orphan"))
    line = Column(Text(), nullable=True)

    t_created = Column(DateTime, default=datetime.now)

class ABICheck(Base):
    __tablename__ = 'abicheck'
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey('request.id'), nullable=False)
    request = relationship(Request, backref=backref('abichecks', order_by=id, cascade="all, delete-orphan"))

    src_project = Column(String(255), nullable=False)
    src_package = Column(String(255), nullable=False)
    src_rev = Column(String(255), nullable=True)
    dst_project = Column(String(255), nullable=False)
    dst_package = Column(String(255), nullable=False)
    result = Column(Boolean(), nullable = True)

    t_created = Column(DateTime, default=datetime.now)
    t_updated = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class LibReport(Base):
    __tablename__ = 'libreport'
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey('abicheck.id'), nullable=False)
    abicheck = relationship(ABICheck, backref=backref('reports', order_by=id, cascade="all, delete-orphan"))

    src_repo = Column(String(255), nullable=False)
    src_lib = Column(String(255), nullable=False)
    dst_repo = Column(String(255), nullable=False)
    dst_lib = Column(String(255), nullable=False)
    arch = Column(String(255), nullable=False)
    htmlreport = Column(String(255), nullable=False)
    result = Column(Boolean(), nullable = False)

    t_created = Column(DateTime, default=datetime.now)
    t_updated = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class Config(Base):
    __tablename__ = 'config'
    id = Column(Integer, primary_key=True)
    key = Column(String(32), nullable=False, unique=True)
    value = Column(String(255), nullable=False)

    t_created = Column(DateTime, default=datetime.now)
    t_updated = Column(DateTime, default=datetime.now, onupdate=datetime.now)

def db_engine():
    return create_engine(f'sqlite:///{DATADIR}/abi-checker.db')

def db_session():
    engine = db_engine()
    Base.metadata.bind = engine
    DBSession = sessionmaker(bind=engine)
    return DBSession()
