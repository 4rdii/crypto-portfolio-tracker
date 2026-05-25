
  import { createRoot } from "react-dom/client";
  import { DynamicContextProvider } from "@dynamic-labs/sdk-react-core";
  import { EthereumWalletConnectors } from "@dynamic-labs/ethereum";
  import { SolanaWalletConnectors } from "@dynamic-labs/solana";
  import App from "./app/App.tsx";
  import "./styles/index.css";

  createRoot(document.getElementById("root")!).render(
    <DynamicContextProvider
      settings={{
        environmentId: "b9921ab2-776a-4e90-a147-0d4e9f92be82",
        walletConnectors: [EthereumWalletConnectors, SolanaWalletConnectors],
        initialAuthenticationMode: "connect-only",
      }}
    >
      <App />
    </DynamicContextProvider>
  );
