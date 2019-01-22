import numpy as np
from scipy.sparse import coo_matrix, csr_matrix, eye, hstack, vstack, bmat, spdiags
from numpy.linalg import norm
from scipy.sparse.linalg import cg, inv, spsolve
from ..functionspace.lagrange_fem_space import LagrangeFiniteElementSpace
from ..functionspace.lagrange_fem_space import VectorLagrangeFiniteElementSpace
from ..mesh import TriangleMesh

class DarcyForchheimerP0P1MGModel:

    def __init__(self, pde, mesh, n):

        self.integrator1 = mesh.integrator(3)
        self.integrator0 = mesh.integrator(1)
        self.pde = pde
        self.uspaces = []
        self.pspaces = []
        self.IMatrix = []
        self.A = []
        self.b = []
        self.uu = []
        self.pp = []

        mesh0 = TriangleMesh(mesh.node, mesh.ds.cell)
        uspace = VectorLagrangeFiniteElementSpace(mesh0, p=0, spacetype='D')
        self.uspaces.append(uspace)

        pspace = LagrangeFiniteElementSpace(mesh0, p=1, spacetype='C')
        self.pspaces.append(pspace)

        for i in range(n):
            I0, I1 = mesh.uniform_refine(returnim=True)
            self.IMatrix.append((I0[0], I1[0]))
            mesh0 = TriangleMesh(mesh.node, mesh.ds.cell)
            uspace = VectorLagrangeFiniteElementSpace(mesh0, p=0, spacetype='D')
            self.uspaces.append(uspace)
            pspace = LagrangeFiniteElementSpace(mesh0, p=1, spacetype='C')
            self.pspaces.append(pspace)

        self.uh = self.uspaces[-1].function()
        self.ph = self.pspaces[-1].function()
        self.uI = self.uspaces[-1].interpolation(pde.velocity)
        self.pI = self.pspaces[-1].interpolation(pde.pressure)

        self.nlevel = n + 1

        #self.A = self.get_linear_stiff_matrix()
        #self.b = self.get_right_vector()
        
    def get_linear_stiff_matrix(self, level):
        
        mesh = self.pspaces[level].mesh
        pde = self.pde
        mu = pde.mu
        rho = pde.rho

        bc = np.array([1/3,1/3,1/3], dtype=mesh.ftype)##weight
        gphi = self.pspaces[level].grad_basis(bc)
        cellmeasure = mesh.entity_measure('cell')

        NC = mesh.number_of_cells()
        NN = mesh.number_of_nodes()
        scaledArea = mu/rho*cellmeasure

        A11 = spdiags(np.repeat(scaledArea, 2), 0, 2*NC, 2*NC)

        phi = self.uspaces[level].basis(bc)
        A21 = np.einsum('ijm, km, i->ijk', gphi, phi, cellmeasure)

        cell2dof0 = self.uspaces[level].cell_to_dof()
        ldof0 = self.uspaces[level].number_of_local_dofs()
        cell2dof1 = self.pspaces[level].cell_to_dof()
        ldof1 = self.pspaces[level].number_of_local_dofs()
		
        gdof0 = self.uspaces[level].number_of_global_dofs()
        gdof1 = self.pspaces[level].number_of_global_dofs()
        I = np.einsum('ij, k->ijk', cell2dof1, np.ones(ldof0))
        J = np.einsum('ij, k->ikj', cell2dof0, np.ones(ldof1))

        A21 = csr_matrix((A21.flat, (I.flat, J.flat)), shape=(gdof1, gdof0))
        A12 = A21.transpose()

        A = bmat([(A11, A12), (A21, None)], format='csr', dtype=np.float)
        
        return A
        
    def get_right_vector(self, level):
        mesh = self.pspaces[level].mesh
        pde = self.pde
        mu = pde.mu
        rho = pde.rho
        NN = mesh.number_of_nodes()
        NC = mesh.number_of_cells()
        cellmeasure = mesh.entity_measure('cell')
        
        f = self.uspaces[level].source_vector(self.pde.f, self.integrator0, cellmeasure)
        b = self.pspaces[level].source_vector(self.pde.g, self.integrator1, cellmeasure)
        	
	## Neumann boundary condition
        node = mesh.entity('node')
        edge = mesh.entity('edge')
        ec = mesh.entity_barycenter('edge')
        isBDEdge = mesh.ds.boundary_edge_flag()
        edge2node = mesh.ds.edge_to_node()
        bdEdge = edge[isBDEdge, :]
        d = np.sqrt(np.sum((node[edge2node[isBDEdge, 0], :]\
            - node[edge2node[isBDEdge, 1], :])**2, 1))
        mid = ec[isBDEdge, :]

        ii = np.tile(d*self.pde.neumann(mid)/2,(1,2))
        g = np.bincount(np.ravel(bdEdge,'F'), weights = np.ravel(ii), minlength=NN)
		
        g = g - b  

        b1 = np.r_[f, g]
        return b1
    

    def compute_initial_value(self):
        mesh = self.pspaces[-1].mesh
        pde = self.pde
        NC = mesh.number_of_cells()
        NN = mesh.number_of_nodes()
        cell = mesh.entity('cell')
        cellmeasure = mesh.entity_measure('cell')
        A = self.get_linear_stiff_matrix(-1)
        b = self.get_right_vector(-1)
        
        up = np.zeros(2*NC+NN, dtype=np.float)
        idx = np.arange(2*NC+NN-1)
        up[idx] = spsolve(A[idx, :][:, idx], b[idx])

        u = up[:2*NC]
        p = up[2*NC:]
        c = np.sum(np.mean(p[cell], 1)*cellmeasure)/np.sum(cellmeasure)
        p -= c

        return u,p

    def prev_smoothing(self, u, p, level, maxN):
        mesh = self.pspaces[level].mesh
        NC = mesh.number_of_cells()
        NN = mesh.number_of_nodes()
        cell = mesh.entity('cell')
        
        mu = self.pde.mu
        rho = self.pde.rho
        beta = self.pde.beta
        alpha = self.pde.alpha
        tol = self.pde.tol
        cellmeasure = mesh.entity_measure('cell')
        area = np.repeat(cellmeasure,2)

        # 每层的A，b
        A = self.get_linear_stiff_matrix(level)
        A11 = A[:2*NC, :2*NC]
        A12 = A[:2*NC, 2*NC:]
        A21 = A[2*NC:, :2*NC]
        b = self.get_right_vector(level)
        ## P-R interation for D-F equation
        n = 0
        ru1 = np.ones(maxN+1, dtype=np.float)
        rp1 = np.ones(maxN+1, dtype=np.float)
        Aalpha = A11 + spdiags(area/alpha, 0, 2*NC, 2*NC)
        Aalphainv = spdiags(1/Aalpha.data, 0, 2*NC, 2*NC)
        
        
        while ru1[n]+rp1[n] > tol and n < maxN:
            ## step 1: Solve the nonlinear Darcy equation
            # Knowing (u,p), explicitly compute the intermediate velocity u(n+1/2)
            F = u/alpha - (mu/rho)*u - (A12@p - b[:2*NC])/area
            FL = np.sqrt(F[::2]**2 + F[1::2]**2)
            gamma = 1.0/(2*alpha) + np.sqrt((1.0/alpha**2) + 4*(beta/rho)*FL)/2
            uhalf = F/np.repeat(gamma, 2)
            
            ## Step 2: Solve the linear Darcy equation
            # update RHS
            uhalfL = np.sqrt(uhalf[::2]**2 + uhalf[1::2]**2)
            fnew = b[:2*NC] + uhalf*area/alpha\
                    - beta/rho*uhalf*np.repeat(uhalfL, 2)*area
            
            ## Direct Solver
            Aalphainv = spdiags(1/Aalpha.data, 0, 2*NC, 2*NC)
            Ap = A21@Aalphainv@A12
           # print('Ap',Ap.toarray())
            bp = A21@(Aalphainv@fnew) - b[2*NC:]
           # print('bp', bp)
            p1 = np.zeros(NN,dtype=np.float)
            p1[1:] = spsolve(Ap[1:,1:],bp[1:])
            c = np.sum(np.mean(p1[cell],1)*cellmeasure)/np.sum(cellmeasure)
            p1 -= c
            u1 = Aalphainv@(fnew - A12@p1)
            
            ## Updated residual and error of consective iterations
            r[0,n] = ru
            r[1,n] = rp
            n = n + 1
            uLength = np.sqrt(u1[::2]**2 + u1[1::2]**2)
            Lu = A11@u1 + (beta/rho)*np.repeat(uLength*cellmeasure, 2)*u1 + A12@p1
            ru = norm(b[:2*NC] - Lu)/norm(b[:2*NC])
            if norm(b[2*NC:]) == 0:
                rp = norm(b[2*NC:] - A21@u1)
            else:
                rp = norm(b[2*NC:] - A21@u1)/norm(b[2*NC:])
            eu = np.max(abs(u1 - self.uh0))
            ep = np.max(abs(p1 - self.ph0))

            h[:] = u1
            p[:] = p1
            
            self.uu = u1.append(u1)                            
            self.pp = p1.append(p1)                    
        
        return u, p, ru, rp

    def post_smoothing(self, u, p, level):
        mesh = self.pspaces[level].mesh
        NC = mesh.number_of_cells()
        NN = mesh.number_of_nodes()
        mu = self.pde.mu
        rho = self.pde.rho
        beta = self.pde.beta
        alpha = self.pde.alpha
        tol = self.tol
        maxN = self.pde.mg_maxN
        cellmeasure = mesh.entity_measure('cell')
        area = np.repeat(cellmeasure,2)
        
        # 每层的A，b
        A = self.get_linear_stiff_matrix(level)
        A11 = A[:2*NC, :2*NC]
        A12 = A[:2*NC, 2*NC:]
        A21 = A[2*NC:, :2*NC]
        b = self.get_right_vector(level)
        
        ## P-R interation for D-F equations

        n = 0
        ru1 = np.ones(maxN,)
        rp1 = np.ones(maxN,)
        uhalf[:] = uh
    
        Aalphainv = A11 + spdiags(area/alpha, 0, 2*NC, 2*NC)
        
        while ru1[n]+rp1[n] > tol and n < maxN:
            ## step 2: Solve the linear Darcy equation
            # update RHS
            uhalfL = np.sqrt(uhalf[::2]**2 + uhalf[1::2]**2)
            fnew = b[:2*NC] + uhalf*area/alpha\
                    - beta/rho*uhalf*np.repeat(uhalfL, 2)*area
            
            ## Direct Solver
            Aalphainv = spdiags(1/Aalpha.data, 0, 2*NC, 2*NC)
            Ap = A21@Aalphainv@A12
           # print('Ap',Ap.toarray())
            bp = A21@(Aalphainv@fnew) - b[2*NC:]
           # print('bp', bp)
            p1 = np.zeros(NN,dtype=np.float)
            p1[1:] = spsolve(Ap[1:,1:],bp[1:])
            c = np.sum(np.mean(p1[cell],1)*cellmeasure)/np.sum(cellmeasure)
            p1 -= c
            u1 = Aalphainv@(fnew - A12@p1)
            
            ## step 1: Solve the nonlinear Darcy equation
            # Knowing (u,p), explicitly compute the intermediate velocity u(n+1/2)
            F = u/alpha - (mu/rho)*u - (A12@p - b[:2*NC])/area
            FL = np.sqrt(F[::2]**2 + F[1::2]**2)
            gamma = 1.0/(2*alpha) + np.sqrt((1.0/alpha**2) + 4*(beta/rho)*FL)/2
            uhalf = F/np.repeat(gamma, 2)
            
            ## Updated residual and error of consective iterations
            r[0,n] = ru
            r[1,n] = rp
            n = n + 1
            uLength = np.sqrt(u1[::2]**2 + u1[1::2]**2)
            Lu = A11@u1 + (beta/rho)*np.repeat(uLength*cellmeasure, 2)*u1 + A12@p1
            ru = norm(b[:2*NC] - Lu)/norm(b[:2*NC])
            if norm(b[2*NC:]) == 0:
                rp = norm(b[2*NC:] - A21@u1)
            else:
                rp = norm(b[2*NC:] - A21@u1)/norm(b[2*NC:])
            eu = np.max(abs(u1 - self.uh0))
            ep = np.max(abs(p1 - self.ph0))

            h[:] = u1
            p[:] = p1
            
        return u, p, ru, rp

    def fas(self):
        u, p = self.compute_initial_value()
        
        mesh = self.pspaces[level].mesh
        NC = mesh.number_of_cells()
        NN = mesh.number_of_nodes()
        mu = self.pde.mu
        rho = self.pde.rho
        beta = self.pde.beta
        alpha = self.pde.alpha
        tol = self.tol
        # coarsest level: exact solve
        if level == 1:##???
            u,p,ru,rp = prev_smoothing(self, self.uu[-1], self.pp[-1], level, self.pde.mg_maxN)
            rn = ru[end] + rp[end]
            return u,p
            
        ## Presmoothing
        u,p,ru, rp = prev_smoothing(self, self.uu[level], self.pp[level], level, self.pde.mg_maxN)
        
        # form residual on the fine grid
        # 每层的A，b
        A = self.get_linear_stiff_matrix(level)
        A11 = A[:2*NC, :2*NC]
        A12 = A[:2*NC, 2*NC:]
        #A21 = A[2*NC:, :2*NC]
        b = self.get_right_vector(level)
        uLength = np.sqrt(u[::2]**2 + u[1::2]**2)
        Lu = A11@u1 + (beta/rho)*np.repeat(uLength*cellmeasure, 2)*u1 + A12@p1
        r = b[:2*NC] - Lu
        
        # restrict residual to the coarse grid
        rc = self.IMatrix[level].T@r
        uc = (self.IMatrix[level].T@u)/4
        pc = p[:Nc]
        
        return u, p
