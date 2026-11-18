"""Machine-dog SSH registration state machine and broker integration."""
from datetime import datetime, timezone

from ssh_bindings import SSHBinding
from ssh_portbroker import PortBrokerError


class SSHRegistrationError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


class SSHRegistrationService:
    def __init__(self, store, broker):
        self.store = store
        self.broker = broker

    async def close(self):
        await self.broker.close()
        await self.store.close()

    async def register(self, device_id, payload):
        existing = await self.store.get_by_device(device_id)
        reused = self._active(existing)
        if self._active(existing):
            host = existing.host
            public_ssh_port = existing.public_ssh_port
            remote_port = existing.port
            version = existing.binding_version
        else:
            public = await self._broker_call(self.broker.public_target)
            occupancy = await self._broker_call(self.broker.get_dog_port, device_id)
            if occupancy and occupancy.get('port'):
                remote_port = int(occupancy['port'])
                reused = True
            else:
                allocated = await self._broker_call(
                    self.broker.allocate, device_id, 'register')
                remote_port = int(allocated['port'])
                public = allocated.get('public') or public
                reused = bool(allocated.get('reused'))
            host = str(public['public_host'])
            public_ssh_port = int(public['public_ssh_port'])
            version = '1'
            existing = None

        now = datetime.now(timezone.utc)
        binding = SSHBinding(
            device_id=device_id,
            device_name=payload['device_name'],
            device_model=payload['device_model'],
            serial_number=payload.get('serial_number'),
            firmware_version=payload.get('firmware_version'),
            device_enabled=bool(payload.get('device_enabled', True)),
            online=True,
            host=host,
            public_ssh_port=public_ssh_port,
            port=remote_port,
            username=payload.get('username') or (
                existing.username if existing is not None else 'robot'
            ),
            status='bound',
            binding_version=version,
            last_seen_at=now,
            updated_at=now,
        )
        saved = await self.store.upsert(binding)
        return self.target(saved, reused=reused)

    async def heartbeat(self, device_id):
        binding = await self.store.get_by_device(device_id)
        if not self._active(binding):
            raise SSHRegistrationError(404, 'NOT_FOUND', 'device has no active SSH binding')
        saved = await self.store.set_online(device_id, True)
        return self.target(saved, reused=True)

    async def offline(self, device_id):
        binding = await self.store.get_by_device(device_id)
        if binding is None:
            raise SSHRegistrationError(404, 'NOT_FOUND', 'device not found')
        saved = await self.store.set_online(device_id, False)
        return {
            **self.target(saved, reused=True),
            'online': False,
            'portPreserved': saved.port is not None,
        }

    async def unbind(self, device_id, operator):
        binding = await self.store.get_by_device(device_id)
        if not self._active(binding):
            raise SSHRegistrationError(404, 'NOT_FOUND', 'device has no active SSH binding')
        released = await self._broker_call(
            self.broker.release, device_id, binding.port, 'destroy')
        unbound, old_port = await self.store.unbind(device_id)
        if unbound is None:
            raise SSHRegistrationError(409, 'CONFLICT', 'binding changed during unbind')
        return {
            'deviceId': device_id,
            'released': True,
            'port': old_port,
            'operator': operator,
            'reason': 'destroy',
            'broker': released,
            'status': 'unbound',
        }

    async def _broker_call(self, function, *args):
        try:
            return await function(*args)
        except PortBrokerError as error:
            status = 409 if error.status == 409 else 503
            code = 'CONFLICT' if status == 409 else 'SERVICE_UNAVAILABLE'
            raise SSHRegistrationError(status, code, str(error)) from error

    @staticmethod
    def _active(binding):
        return (
            binding is not None
            and binding.port is not None
            and binding.status != 'unbound'
        )

    @staticmethod
    def target(binding, reused):
        return {
            'deviceId': binding.device_id,
            'deviceName': binding.device_name,
            'deviceModel': binding.device_model,
            'online': binding.online,
            'publicHost': binding.host,
            'publicSshPort': binding.public_ssh_port,
            'remotePort': binding.port,
            'sshUser': binding.username,
            'bindingVersion': binding.binding_version,
            'reused': reused,
        }
