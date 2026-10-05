"""API Router"""
from fastapi import APIRouter

from app.api.v1.endpoints import vms, vm_devices, storage, networks, hosts, tasks, events, groups, clusters

api_router = APIRouter()

# Before vms: its POST /{vm_id}/{action} would otherwise catch POST /{vm_id}/disks
api_router.include_router(vm_devices.router, prefix="/vms", tags=["vms"])
api_router.include_router(vms.router, prefix="/vms", tags=["vms"])
api_router.include_router(storage.router, prefix="/storage", tags=["storage"])
api_router.include_router(networks.router, prefix="/networks", tags=["networks"])
api_router.include_router(hosts.router, prefix="/hosts", tags=["hosts"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(events.router, prefix="/events", tags=["events"])
api_router.include_router(groups.router, prefix="/groups", tags=["groups"])
api_router.include_router(clusters.router, prefix="/clusters", tags=["clusters"])
