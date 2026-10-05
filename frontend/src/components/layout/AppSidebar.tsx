import React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Nav, NavItem, NavList, PageSidebar, PageSidebarBody } from '@patternfly/react-core';
import {
  TachometerAltIcon,
  VirtualMachineIcon,
  StorageDomainIcon,
  NetworkIcon,
  ServerIcon,
  TaskIcon,
  ClusterIcon,
} from '@patternfly/react-icons';

const NAV_ITEMS = [
  { path: '/', label: 'Dashboard', icon: TachometerAltIcon },
  { path: '/vms', label: 'Virtual Machines', icon: VirtualMachineIcon },
  { path: '/storage', label: 'Storage', icon: StorageDomainIcon },
  { path: '/networks', label: 'Networks', icon: NetworkIcon },
  { path: '/clusters', label: 'Clusters', icon: ClusterIcon },
  { path: '/hosts', label: 'Host', icon: ServerIcon },
  { path: '/tasks', label: 'Tasks', icon: TaskIcon },
];

export const AppSidebar: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();

  return (
    <PageSidebar>
      <PageSidebarBody>
        <Nav aria-label="Main navigation">
          <NavList>
            {NAV_ITEMS.map(({ path, label, icon: Icon }) => (
              <NavItem
                key={path}
                itemId={path}
                to={path}
                isActive={location.pathname === path}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(path);
                }}
              >
                <Icon style={{ marginRight: 8 }} />
                {label}
              </NavItem>
            ))}
          </NavList>
        </Nav>
      </PageSidebarBody>
    </PageSidebar>
  );
};
