from __future__ import annotations

from application_agent.ats.ashby import AshbyAdapter
from application_agent.ats.generic import GenericAdapter
from application_agent.ats.greenhouse import GreenhouseAdapter
from application_agent.ats.icims import ICIMSAdapter
from application_agent.ats.lever import LeverAdapter
from application_agent.ats.oracle import OracleAdapter
from application_agent.ats.smartrecruiters import SmartRecruitersAdapter
from application_agent.ats.successfactors import SuccessFactorsAdapter
from application_agent.ats.taleo import TaleoAdapter
from application_agent.ats.workday import WorkdayAdapter


def get_adapter(ats: str):
    return {
        "greenhouse": GreenhouseAdapter(),
        "lever": LeverAdapter(),
        "ashby": AshbyAdapter(),
        "workday": WorkdayAdapter(),
        "oracle": OracleAdapter(),
        "taleo": TaleoAdapter(),
        "icims": ICIMSAdapter(),
        "smartrecruiters": SmartRecruitersAdapter(),
        "successfactors": SuccessFactorsAdapter(),
    }.get(ats, GenericAdapter())
