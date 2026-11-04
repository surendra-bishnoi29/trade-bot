import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";
import { Chart, ChartSpec } from "./Chart";

/** The head trader writes Markdown. Tables and ==highlights== render; a fenced `chart` block with a JSON
 *  spec draws a price chart with the thesis levels. Raw HTML other than <mark> is dropped. */
function prepare(text: string): string {
  return text
    .replace(/<\/?(script|iframe|object|embed|style)[^>]*>/gi, "")
    .replace(/==([^=\n]+)==/g, "<mark>$1</mark>");
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw]}
        components={{
          code({ className, children, ...props }) {
            const lang = /language-(\w+)/.exec(className ?? "")?.[1];
            const body = String(children).trim();
            if (lang === "chart") {
              try {
                return <Chart spec={JSON.parse(body) as ChartSpec} />;
              } catch {
                return <pre className="code">chart: invalid spec</pre>;
              }
            }
            if (!className && !body.includes("\n")) {
              return (
                <code className="inline" {...props}>
                  {children}
                </code>
              );
            }
            return <pre className="code">{body}</pre>;
          },
          a({ href, children }) {
            return (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
          table({ children }) {
            return (
              <div className="table-wrap">
                <table>{children}</table>
              </div>
            );
          },
        }}
      >
        {prepare(text)}
      </ReactMarkdown>
    </div>
  );
}
