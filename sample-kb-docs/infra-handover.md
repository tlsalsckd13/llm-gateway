# Infra Handover

AI PoC gateway는 ALB, nginx-proxy, gateway-web, usage-collector, Portkey Gateway로 구성된다.

운영 중 긴 Claude Code 작업은 ALB idle timeout, nginx proxy timeout, Bedrock read timeout을 모두 길게 유지해야 한다.
